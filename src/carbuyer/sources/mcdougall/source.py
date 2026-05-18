"""McDougall Auctioneers source plugin.

Covers mcdougallauction.com's Vehicles + Vocational Trucks taxonomy.
Selectors are placeholders pending fixture capture; structural plugin enables routing.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, ClassVar, Self

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
from carbuyer.sources.mcdougall.parser import (
    CatalogEntry,
    parse_catalog_page,
    parse_lot_detail,
)
from carbuyer.sources.resolver import canonicalize_url
from carbuyer.sources.retry import RetryTransport

_log = get_logger("sources.mcdougall")

VEHICLES_URL = "https://www.mcdougallauction.com/vehicles"
CATALOG_URL = "https://www.mcdougallauction.com/products.php?category=Vehicles"
# Defensive ceiling on pagination — bounds the worst-case ingest length if
# McDougall ever serves an unbounded duplicate page or our empty-page sentinel
# fails. 50 pages * ~14 lots/page = 700 lots, well above current ~150 inventory.
_CATALOG_MAX_PAGES = 50
# 404 and 410 both mean "this lot URL is no longer served" — the poller must
# surface "missing" so the scheduler advances the lot to CLOSED. Without 410
# in this set, a McDougall switch to RFC-compliant permanent-removal status
# would burn 30s polling slots per lot until the 24h force-close guard fires
# (same bug class HiBid was patched for in commit 24ddef1).
_LOT_GONE_STATUS_CODES: tuple[int, ...] = (
    int(httpx.codes.NOT_FOUND),
    int(httpx.codes.GONE),
)

# Auction detail pages: auction-event.php?arg=<GUID-8-4-4-4-12>.
# Lot detail pages:    products-full-view.php?arg=<GUID-8-4-4-4-12>.
# Cross-auction Vehicles catalog: products.php?category=Vehicles.
_GUID_RE = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_MCDOUGALL_AUCTION_URL = re.compile(
    rf"^https?://(?:www\.)?mcdougallauction\.com/auction-event\.php\?[^#]*\barg=({_GUID_RE})",
)

# "<addr>, <city>, <prov>" requires the trailing two comma-separated parts to
# look like city + province; everything before joins as the address. Anything
# with fewer than three parts is treated as opaque -- whole string goes into
# pickup_address, city/province None. The catalog format is consistent enough
# that this works for current McDougall inventory; revisit if multi-yard
# addresses start appearing with varied shapes.
_MIN_PICKUP_PARTS = 3


def _split_pickup_location(value: str) -> tuple[str | None, str | None, str | None]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) < _MIN_PICKUP_PARTS:
        return (value.strip() or None, None, None)
    province = parts[-1]
    city = parts[-2]
    address = ", ".join(parts[:-2])
    return (address or None, city or None, province or None)


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

    async def discover_vehicle_lots(self) -> AsyncIterator[tuple[RawAuction, RawLot]]:
        """Lot-first cross-auction ingestion: catalog walker -> fetch_lot per
        entry -> (RawAuction, RawLot) pair. Mirrors HiBid's discover_vehicle_lots
        interface so the ingester dispatch can wire both sources the same way.

        Auction-level metadata for first-seen GUIDs is built from RawLot.extra
        (which carries auction_guid + pickup_location + premium terms) -- no
        separate fetch to auction-event.php in the per-lot loop. Auction title
        / scheduled_start_at / scheduled_end_at remain None until we ever add
        an auction-event fetcher; for now the lot's enrichment + valuation
        signal carries the user-visible value.

        One fetch_lot failure is logged and skipped; sibling lots continue.
        """
        async for entry in self.iter_catalog_entries():
            placeholder_ref = LotRef(
                source=self.name,
                source_auction_id="",  # filled below once we have the GUID
                source_lot_id=entry.lot_guid,
                url=entry.lot_url,
            )
            try:
                raw_lot = await self.fetch_lot(placeholder_ref)
            except Exception:
                _log.exception(
                    "fetch_lot failed",
                    lot_guid=entry.lot_guid, url=entry.lot_url,
                )
                continue
            auction_guid = raw_lot.extra.get("auction_guid")
            if not isinstance(auction_guid, str) or not auction_guid:
                _log.warning(
                    "lot has no parent auction GUID; skipping",
                    lot_guid=entry.lot_guid,
                )
                continue
            resolved_ref = replace(placeholder_ref, source_auction_id=auction_guid)
            final_lot = replace(raw_lot, ref=resolved_ref)
            raw_auction = self._build_raw_auction(auction_guid, raw_lot.extra)
            yield raw_auction, final_lot

    def _build_raw_auction(
        self, auction_guid: str, lot_extras: dict[str, Any],
    ) -> RawAuction:
        """Build a minimum-viable RawAuction from per-lot extras.

        Title and scheduled dates are None because the lot detail page
        doesn't expose them; they would require fetching auction-event.php
        per auction. The dashboard surfaces lots with the parent auction's
        pickup info instead, and the auction title slot gets a stable
        placeholder operators can recognise.
        """
        pickup = lot_extras.get("pickup_location")
        addr, city, prov = _split_pickup_location(pickup) if pickup else (None, None, None)
        auction_url = f"https://www.mcdougallauction.com/auction-event.php?arg={auction_guid}"
        return RawAuction(
            ref=AuctionRef(
                source=self.name,
                source_auction_id=auction_guid,
                url=canonicalize_url(auction_url),
            ),
            title=None,
            description=None,
            auctioneer_name="McDougall Auctioneers",
            auctioneer_external_id=None,
            scheduled_start_at=None,
            scheduled_end_at=None,
            pickup_address=addr,
            pickup_city=city,
            pickup_province=prov,
            pickup_window_text=None,
            buyer_premium_pct=lot_extras.get("buyer_premium_pct"),
            online_bidding_fee_pct=None,
            terms_text=None,
            auction_subtype="estate",
            buyer_premium_max_cad=lot_extras.get("buyer_premium_max_cad"),
            buyer_premium_min_cad=lot_extras.get("buyer_premium_min_cad"),
        )

    async def iter_catalog_entries(
        self,
        *,
        max_pages: int = _CATALOG_MAX_PAGES,
    ) -> AsyncIterator[CatalogEntry]:
        """Walk every page of products.php?category=Vehicles, yielding one
        CatalogEntry per lot card. Stops on first empty page (sentinel) or
        when max_pages is reached.

        Yields summary data only. Parent-auction GUID + lot-detail fields
        (mileage, VIN, photos) require the detail-page fetcher (commit #6).
        """
        for page in range(1, max_pages + 1):
            url = CATALOG_URL if page == 1 else f"{CATALOG_URL}&p={page}"
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
            except Exception as exc:
                _log.warning(
                    "catalog page fetch failed",
                    page=page, url=url, error=str(exc),
                )
                # One bad page must not abort the whole walk — return what
                # we have so far. Distinct from "empty page" semantically.
                return
            entries = parse_catalog_page(resp.text)
            if not entries:
                return
            for entry in entries:
                yield entry
            if page < max_pages:
                await jittered_sleep()

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
        """Fetch one products-full-view.php?arg=<GUID> detail page.

        year/make/model are intentionally left None — extracted later by the
        enricher's LLM pass against ``title`` + ``description`` (same pattern
        as HiBid). Parent auction GUID + pickup location come along inside
        ``extra`` so the ingester (commit #8) can build a RawAuction from
        the same fetch without a second HTTP round to auction-event.php.
        """
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        detail = parse_lot_detail(resp.text, lot_guid=ref.source_lot_id)
        extra: dict[str, Any] = {
            **detail.extras,
            "pickup_location": detail.pickup_location,
            "auction_guid": detail.auction_guid,
            "buyer_premium_pct": detail.buyer_premium_pct,
            "buyer_premium_max_cad": detail.buyer_premium_max_cad,
            "buyer_premium_min_cad": detail.buyer_premium_min_cad,
        }
        return RawLot(
            ref=ref,
            lot_number=None,
            title=detail.title,
            description=detail.description,
            photos=detail.photos,
            mileage_km=detail.mileage_km,
            vin=detail.vin,
            current_high_bid_cad=detail.current_high_bid_cad,
            bid_count_visible=detail.bid_count_visible,
            scheduled_end_at=detail.scheduled_end_at,
            extra=extra,
        )

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        """Re-fetch one lot detail page; emit current bid + end_time.

        Status detection v1: HTTP 404 or 410 -> "missing" (lot removed). 200 with
        a parseable end_time -> "open" regardless of whether that time has
        passed -- the bid_poller's force-close-by-scheduled-end logic owns
        the OPEN -> CLOSED transition until we capture a closed-lot fixture
        and find an in-page closed marker. McDougall has soft-close, so a
        lot can legitimately remain open past its scheduled end while final
        bids land.
        """
        observed = datetime.now(UTC)
        resp = await self._http.get(ref.url)
        if resp.status_code in _LOT_GONE_STATUS_CODES:
            return BidObservation(
                ref=ref,
                observed_at=observed,
                current_high_bid_cad=None,
                end_time_at_observation=None,
                status_at_observation="missing",
            )
        resp.raise_for_status()
        detail = parse_lot_detail(resp.text, lot_guid=ref.source_lot_id)
        return BidObservation(
            ref=ref,
            observed_at=observed,
            current_high_bid_cad=detail.current_high_bid_cad,
            end_time_at_observation=detail.scheduled_end_at,
            status_at_observation="open",
        )


# Register at import time so the ingester / dashboard health view can
# enumerate covered platforms via SOURCES.
register(McDougallSource())
