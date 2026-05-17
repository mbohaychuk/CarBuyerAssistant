"""farmauctionguide.com platform router.

farmauctionguide.com aggregates upcoming auctions across many auctioneers and
platforms. This plugin walks per-province listing pages, inspects every outbound
link, and identifies the underlying auction platform.

Known platforms (hibid, mcdougall) get emitted as AuctionRef(source="hibid"|
"mcdougall", ...) so the respective plugin's fetch_auction/fetch_lots/poll_bid
takes over. Unknown platforms get emitted as AuctionRef(source="unknown:<host>",
...) so the auction appears in the dashboard but no lot-scraper is dispatched.

This is a pure router: fetch_auction / fetch_lots / fetch_lot / poll_bid are
no-ops. The discoverer-side dispatch in _sweep_one_discoverer handles routing
to the correct plugin based on ref.source, so these methods are never called in
production for known-platform refs.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import ClassVar, Self
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser

from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.sources.base import (
    AuctionRef,
    AuctionSource,
    BidObservation,
    LotRef,
    RawAuction,
    RawLot,
    register,
)
from carbuyer.sources.http import jittered_sleep, make_client
from carbuyer.sources.retry import RetryTransport

_log = get_logger("sources.farmauctionguide")

# Per-province listing pages. Western Canada matches hibid_provinces default.
PROVINCE_PAGES: dict[str, str] = {
    "AB": "https://www.farmauctionguide.com/canada/alberta/",
    "SK": "https://www.farmauctionguide.com/canada/saskatchewan/",
    "MB": "https://www.farmauctionguide.com/canada/manitoba/",
    "BC": "https://www.farmauctionguide.com/canada/british-columbia/",
}

# Hosts that belong to farmauctionguide itself — skip internal nav links.
_FAG_HOSTS: frozenset[str] = frozenset({"farmauctionguide.com", "www.farmauctionguide.com"})

# Each tuple: (hostname pattern, resolved source name, regex to extract auction id).
# Patterns are checked in order; first match wins. The id regex is applied to
# `path?query` (not just path) because some platforms (McDougall) carry the id
# as a query parameter, not a path segment.
_GUID_RE = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
PLATFORM_RULES: list[tuple[re.Pattern[str], str, re.Pattern[str]]] = [
    (
        re.compile(r"(^|\.)hibid\.com$", re.I),
        "hibid",
        re.compile(r"/(?:catalog|auctions?)/(\d+)"),
    ),
    (
        re.compile(r"(^|\.)mcdougallauction\.com$", re.I),
        "mcdougall",
        re.compile(rf"/auction-event\.php\?[^#]*\barg=({_GUID_RE})"),
    ),
]


def resolve_platform(url: str) -> tuple[str, str] | None:
    """Map an auction URL to (resolved_source, extracted_auction_id).

    Returns ("hibid"|"mcdougall", "<id>") for known platforms with a valid
    auction id (numeric for HiBid, GUID for McDougall), ("unknown:<host>",
    "<last-path-segment>") for unknown hosts, or None for known hosts whose
    URL doesn't carry an auction id (footer / nav / help pages).

    The None return tells callers to skip the link entirely — emitting it as
    unknown:<known-host> would trigger a spurious needs_plugin alert for a
    platform we already have a plugin for. The "unknown:<host>" convention
    matches _sweep_one_discoverer's existing check
    (ref.source.startswith("unknown:")) without any worker-side changes.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    # Search path + query so query-string auction ids (?arg=<GUID>) match.
    path_and_query = parsed.path + ("?" + parsed.query if parsed.query else "")
    for host_pat, source_name, id_pat in PLATFORM_RULES:
        if host_pat.search(host):
            m = id_pat.search(path_and_query)
            if m:
                return source_name, m.group(1)
            # Host matched a known platform but the URL doesn't carry an
            # auction id — this is a non-auction link (nav, footer, help, etc.).
            # Skip rather than emit as unknown to avoid a needs_plugin alert
            # for a platform we already plug.
            return None
    # Fallback: unknown:<host> with last non-empty path segment as ID.
    fallback_host = host or "unknown_host"
    last_segment = parsed.path.rstrip("/").split("/")[-1]
    fallback_id = last_segment if last_segment else url
    return f"unknown:{fallback_host}", fallback_id


class FarmAuctionGuideSource(AuctionSource):
    """Platform router for farmauctionguide.com.

    Discovers auctions by walking per-province pages and routing outbound links
    to the correct per-platform plugin by emitting refs with the resolved source
    name. Does not own fetch_auction / fetch_lots / poll_bid — those are no-ops.
    """

    name: ClassVar[str] = "farmauctionguide"
    # Bump when discover_auctions contract or PLATFORM_RULES change.
    version: ClassVar[str] = "1"

    def __init__(
        self,
        provinces: list[str],
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provinces = provinces
        # Tests inject a MockTransport; production wires a RetryTransport
        # around an httpx.AsyncHTTPTransport in __aenter__.
        self._injected_transport = _transport
        self._client_cm: AbstractAsyncContextManager[httpx.AsyncClient] | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        transport = self._injected_transport or RetryTransport(
            httpx.AsyncHTTPTransport(),
        )
        self._client_cm = make_client(transport=transport)
        self._client = await self._client_cm.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client_cm is not None:
            await self._client_cm.__aexit__(exc_type, exc, tb)
        self._client_cm = None
        self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "FarmAuctionGuideSource used outside `async with` — wrap in context manager",
            )
        return self._client

    async def discover_auctions(self) -> AsyncIterator[AuctionRef]:
        """Walk per-province pages, route outbound links to their platforms.

        Links pointing back to farmauctionguide.com itself (nav, pagination, etc.)
        are skipped. Each distinct (source, id) pair is emitted at most once per
        sweep to avoid downstream duplicate-upsert noise.
        """
        seen: set[tuple[str, str]] = set()
        for i, province in enumerate(self.provinces):
            page_url = PROVINCE_PAGES.get(province)
            if page_url is None:
                _log.warning("no province page for province", province=province)
                continue
            try:
                resp = await self._http.get(page_url)
                resp.raise_for_status()
            except Exception as exc:
                # One failing province must NOT abort the whole sweep.
                _log.warning(
                    "discover_auctions province failed",
                    province=province,
                    url=page_url,
                    error=str(exc),
                )
                continue
            tree = HTMLParser(resp.text)
            for link in tree.css("a[href]"):
                href = link.attributes.get("href") or ""
                if not href.startswith("http"):
                    # Relative or protocol-relative links are internal nav.
                    continue
                parsed = urlparse(href)
                link_host = parsed.netloc.lower()
                if link_host in _FAG_HOSTS:
                    # Internal farmauctionguide navigation — not an auction link.
                    continue
                resolved = resolve_platform(href)
                if resolved is None:
                    # Known host but no auction-id in path — non-auction link
                    # (footer, nav, help). Skip to avoid spurious needs_plugin alert.
                    continue
                source, ext_id = resolved
                key = (source, ext_id)
                if key in seen:
                    continue
                seen.add(key)
                yield AuctionRef(source=source, source_auction_id=ext_id, url=href)
            if i < len(self.provinces) - 1:
                await jittered_sleep()

    async def fetch_auction(self, ref: AuctionRef) -> RawAuction:
        """No-op — farmauctionguide is a router; per-platform plugins own fetch."""
        # Only reached if a caller invokes this directly; _sweep_one_discoverer
        # routes known-platform refs to their plugin and uses minimal_raw_auction
        # for unknown: refs — never calls this method in production.
        return RawAuction(
            ref=ref,
            title=None,
            description=None,
            auctioneer_name=None,
            auctioneer_external_id=None,
            scheduled_start_at=None,
            scheduled_end_at=None,
            pickup_address=None,
            pickup_city=None,
            pickup_province=None,
            pickup_window_text=None,
            buyer_premium_pct=Decimal("0.10"),
            online_bidding_fee_pct=None,
            terms_text=None,
            auction_subtype="estate",
        )

    async def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]:
        """No-op — farmauctionguide is a router; per-platform plugins own lot extraction."""
        del ref  # unused — farmauctionguide does not own lot extraction
        return
        yield  # unreachable; makes this an async generator at the AST level

    async def fetch_lot(self, ref: LotRef) -> RawLot:
        raise NotImplementedError("farmauctionguide does not own lot extraction")

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        """No-op — farmauctionguide is a router; per-platform plugins own poll."""
        # Unknown-platform auctions never have lots (no lot-scraper to populate
        # them), so the bid-poller naturally has nothing to poll. Returns "missing"
        # as a safe sentinel in case this is ever invoked directly.
        return BidObservation(
            ref=ref,
            observed_at=datetime.now(UTC),
            current_high_bid_cad=None,
            end_time_at_observation=None,
            status_at_observation="missing",
        )


# Register at import time so the lot-scraper / discoverer worker / dashboard
# health view can enumerate covered platforms via SOURCES.
# Reuses hibid_provinces — same Western Canada scope.
register(FarmAuctionGuideSource(provinces=list(settings.hibid_provinces)))
