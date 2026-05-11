"""McDougall Auctioneers source plugin.

Covers mcdougallauction.com's Vehicles + Vocational Trucks taxonomy.
Selectors are placeholders pending fixture capture; structural plugin enables routing.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import ClassVar, Self

import httpx
from selectolax.parser import HTMLParser

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
from carbuyer.sources.resolver import canonicalize_url
from carbuyer.sources.retry import RetryTransport

_log = get_logger("sources.mcdougall")

VEHICLES_URL = "https://www.mcdougallauction.com/vehicles"
_HTTP_NOT_FOUND: int = int(httpx.codes.NOT_FOUND)

# Auction detail pages follow /auction/<numeric-id>[/<slug>].
_MCDOUGALL_AUCTION_URL = re.compile(
    r"^https?://(?:www\.)?mcdougallauction\.com/auction/(\d+)",
)


class McDougallSource(AuctionSource):
    """Source plugin for McDougall Auctioneers (mcdougallauction.com)."""

    name: ClassVar[str] = "mcdougall"
    # Bump when selectors or the discovery/fetch contract changes.
    version: ClassVar[str] = "1"

    @classmethod
    def parse_auction_url(cls, url: str) -> AuctionRef | None:
        """Recognize McDougall auction URLs (/auction/<id>[/<slug>])."""
        m = _MCDOUGALL_AUCTION_URL.match(url)
        if m is None:
            return None
        return AuctionRef(
            source=cls.name,
            source_auction_id=m.group(1),
            url=canonicalize_url(url),
        )

    def __init__(
        self,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
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
                "McDougallSource used outside `async with` — wrap in context manager",
            )
        return self._client

    async def discover_auctions(self) -> AsyncIterator[AuctionRef]:
        seen: set[str] = set()
        resp = await self._http.get(VEHICLES_URL)
        resp.raise_for_status()
        tree = HTMLParser(resp.text)
        # Selector targets links whose href contains "/auction/".
        # Verified against a captured fixture once one is available.
        for node in tree.css("a[href*='/auction/']"):
            href = node.attributes.get("href") or ""
            # Expected href pattern: /auction/<id>[/<slug>]
            m = _MCDOUGALL_AUCTION_URL.search(href) or _MCDOUGALL_AUCTION_URL.search(
                f"https://www.mcdougallauction.com{href}"
            )
            if m is None:
                continue
            auction_id = m.group(1)
            if auction_id in seen:
                continue
            seen.add(auction_id)
            # Reconstruct a canonical URL for absolute and relative hrefs.
            if href.startswith("http"):
                url = canonicalize_url(href)
            else:
                url = canonicalize_url(f"https://www.mcdougallauction.com{href}")
            yield AuctionRef(
                source="mcdougall",
                source_auction_id=auction_id,
                url=url,
            )
        await jittered_sleep()

    async def fetch_auction(self, ref: AuctionRef) -> RawAuction:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        # Auction-level metadata extraction is deferred until a live fixture is
        # captured and selectors are confirmed. Returning conservative defaults
        # lets the worker insert a row and proceed to lot-level scraping.
        return RawAuction(
            ref=ref,
            title=None,
            description=None,
            auctioneer_name="McDougall Auctioneers",
            auctioneer_external_id=None,
            scheduled_start_at=None,
            scheduled_end_at=None,
            pickup_address=None,
            pickup_city=None,
            pickup_province=None,
            pickup_window_text=None,
            buyer_premium_pct=Decimal("0.10"),  # conservative default until confirmed
            online_bidding_fee_pct=None,
            terms_text=None,
            auction_subtype="estate",
        )

    async def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        tree = HTMLParser(resp.text)
        # Lot card selectors are placeholders; confirmed against fixture when captured.
        for node in tree.css("a[href*='/lot/']"):
            href = node.attributes.get("href") or ""
            if not href:
                continue
            # Expect /lot/<id>[/<slug>] — extract numeric segment after /lot/.
            parts = [p for p in href.strip("/").split("/") if p]
            lot_idx = next((i for i, p in enumerate(parts) if p == "lot"), None)
            if lot_idx is None or lot_idx + 1 >= len(parts):
                continue
            lot_id_segment = parts[lot_idx + 1]
            if not lot_id_segment.isdigit():
                continue
            if href.startswith("http"):
                lot_url = canonicalize_url(href)
            else:
                lot_url = canonicalize_url(f"https://www.mcdougallauction.com{href}")
            yield LotRef(
                source="mcdougall",
                source_auction_id=ref.source_auction_id,
                source_lot_id=lot_id_segment,
                url=lot_url,
            )
        await jittered_sleep()

    async def fetch_lot(self, ref: LotRef) -> RawLot:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        # Field-level selectors are placeholders pending fixture capture.
        return RawLot(
            ref=ref,
            lot_number=None,
            title=None,
            description=None,
            photos=[],
            lot_status="open",
        )

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        resp = await self._http.get(ref.url)
        if resp.status_code == _HTTP_NOT_FOUND:
            return BidObservation(
                ref=ref,
                observed_at=datetime.now(UTC),
                current_high_bid_cad=None,
                end_time_at_observation=None,
                status_at_observation="missing",
            )
        resp.raise_for_status()
        # Bid-state selectors are placeholders pending fixture capture.
        return BidObservation(
            ref=ref,
            observed_at=datetime.now(UTC),
            current_high_bid_cad=None,
            end_time_at_observation=None,
            status_at_observation="open",
        )


# Register at import time so the lot-scraper / discoverer worker / dashboard
# health view can enumerate covered platforms via SOURCES.
register(McDougallSource())
