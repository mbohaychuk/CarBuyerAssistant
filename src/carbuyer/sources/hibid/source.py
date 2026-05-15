"""HiBid source plugin (post-SPA migration, May 2026).

HiBid moved the catalog to a SPA backed by a GraphQL endpoint. Layout:

  * Province auction list (https://hibid.com/{province}/auctions/700006/...)
    is still server-rendered HTML — discovery scrapes <a href> patterns.
  * Single auction catalog (https://hibid.com/{province}/catalog/{id}/...)
    is empty of lot data; the page issues one GraphQL POST per piece of
    state (`AuctionDetails`, `LotSearchLotOnly`, `CategorySearch`).
  * Bid polling uses the same `LotSearchLotOnly` query with
    `eventItemIds=[id]` to fetch a single lot's current state.

GraphQL POSTs need a Cloudflare-issued `__cf_bm` cookie. We bootstrap by
hitting any HiBid page once per client lifetime; subsequent POSTs ride
the same cookie jar.
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, ClassVar, Self

import httpx

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
from carbuyer.sources.hibid.parser import (
    HibidLotSummary,
    discover_auction_ids,
    lot_is_closed,
    parse_auction_details_response,
    parse_lot_record,
)
from carbuyer.sources.hibid.urls import catalog_url, lot_url, province_vehicles_url
from carbuyer.sources.http import jittered_sleep, make_client
from carbuyer.sources.resolver import canonicalize_url
from carbuyer.sources.retry import RetryTransport
from carbuyer.shared.config import settings

_log = get_logger("sources.hibid")

_HIBID_CATALOG_URL = re.compile(
    r"^https?://(?:www\.)?hibid\.com/(?:[a-z\-]+/)?catalog/(\d+)",
)

_GRAPHQL_URL = "https://hibid.com/graphql"
# Bootstrap URL hit once per client to set the Cloudflare cookie. Any HiBid
# page works; we use the lightest one we know.
_BOOTSTRAP_URL = "https://hibid.com/"

_LOT_SEARCH_QUERY = """query LotSearchLotOnly(
    $auctionId: Int = null,
    $pageNumber: Int!,
    $pageLength: Int!,
    $status: AuctionLotStatus = null,
    $sortOrder: EventItemSortOrder = null,
    $filter: AuctionLotFilter = null,
    $isArchive: Boolean = false,
    $countAsView: Boolean = true,
    $hideGoogle: Boolean = false,
    $eventItemIds: [Int!] = null
) {
  lotSearch(
    input: {
      auctionId: $auctionId, status: $status, sortOrder: $sortOrder,
      filter: $filter, isArchive: $isArchive, countAsView: $countAsView,
      hideGoogle: $hideGoogle, eventItemIds: $eventItemIds
    }
    pageNumber: $pageNumber
    pageLength: $pageLength
    sortDirection: DESC
  ) {
    pagedResults {
      pageLength pageNumber totalCount filteredCount
      results {
        id itemId lotNumber lead description bidAmount pictureCount shippingOffered
        featuredPicture { fullSizeLocation thumbnailLocation }
        pictures { fullSizeLocation thumbnailLocation }
        lotState {
          highBid minBid bidCount status timeLeft timeLeftSeconds
          isClosed isLive priceRealized reserveSatisfied
        }
        category { id categoryName fullCategory }
      }
    }
  }
}"""

_AUCTION_DETAILS_QUERY = """query AuctionDetails($id: Int!, $countAsView: Boolean = true) {
  auction(id: $id, countAsView: $countAsView) {
    id eventName description
    bidOpenDateTime bidCloseDateTime
    eventAddress eventCity eventState eventZip
    eventDateBegin eventDateEnd
    buyerPremium buyerPremiumRate showBuyerPremium
    lotCount
    auctioneer { id name phone email city state country internetAddress }
    auctionState { auctionStatus openLotCount }
    sourceType
  }
}"""

# Cross-auction lot search — distinct from LotSearchLotOnly because the
# wider LotSearch accepts `state` filter AND we include the parent
# `auction { ... }` sub-selection so the ingester can hydrate auctions
# without a second AuctionDetails round-trip.
_LOT_SEARCH_FULL_QUERY = """query LotSearch(
    $auctionId: Int = null, $category: CategoryId = null,
    $searchText: String = null, $state: String = null,
    $zip: String = null, $miles: Int = null,
    $countryName: String = null, $shippingOffered: Boolean = false,
    $status: AuctionLotStatus = null, $sortOrder: EventItemSortOrder = null,
    $filter: AuctionLotFilter = null, $isArchive: Boolean = false,
    $dateStart: DateTime, $dateEnd: DateTime,
    $countAsView: Boolean = true, $hideGoogle: Boolean = false,
    $eventItemIds: [Int!] = null,
    $pageNumber: Int!, $pageLength: Int!
) {
  lotSearch(
    input: {
      auctionId: $auctionId, category: $category, searchText: $searchText,
      state: $state, zip: $zip, miles: $miles, countryName: $countryName,
      shippingOffered: $shippingOffered, status: $status, sortOrder: $sortOrder,
      filter: $filter, isArchive: $isArchive, dateStart: $dateStart,
      dateEnd: $dateEnd, countAsView: $countAsView, hideGoogle: $hideGoogle,
      eventItemIds: $eventItemIds
    }
    pageNumber: $pageNumber pageLength: $pageLength sortDirection: DESC
  ) {
    pagedResults {
      pageLength pageNumber totalCount filteredCount
      results {
        id itemId lotNumber lead description bidAmount pictureCount shippingOffered
        featuredPicture { fullSizeLocation thumbnailLocation }
        pictures { fullSizeLocation thumbnailLocation }
        lotState {
          highBid minBid bidCount status timeLeft timeLeftSeconds
          isClosed isLive priceRealized reserveSatisfied
        }
        category { id categoryName fullCategory }
        auction {
          id eventName bidCloseDateTime bidOpenDateTime
          eventAddress eventCity eventState eventZip
          buyerPremiumRate
          auctioneer { id name }
        }
      }
    }
  }
}"""

# HiBid's "Cars & Vehicles" top-level category id, used for the
# category-filtered cross-auction lot search.
_VEHICLE_CATEGORY_ID = 700006

_PAGE_SIZE = 100


class HibidSource(AuctionSource):
    name: ClassVar[str] = "hibid"
    # Bump when GraphQL contracts or parser semantics change. 2.0 = post-SPA
    # migration; previously parsed `var lotModels = [...]` (now defunct).
    version: ClassVar[str] = "2.0"

    @classmethod
    def parse_auction_url(cls, url: str) -> AuctionRef | None:
        """Recognize HiBid catalog URLs (with or without province prefix or slug)."""
        m = _HIBID_CATALOG_URL.match(url)
        if m is None:
            return None
        return AuctionRef(
            source=cls.name,
            source_auction_id=m.group(1),
            url=canonicalize_url(url),
        )

    def __init__(
        self,
        provinces: list[str],
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provinces = provinces
        self._injected_transport = _transport
        self._client_cm: AbstractAsyncContextManager[httpx.AsyncClient] | None = None
        self._client: httpx.AsyncClient | None = None
        # Per-auction cache populated by fetch_lots; fetch_lot reads here so
        # we avoid a second GraphQL round-trip per lot. Cleared at the start
        # of each fetch_lots call to stay tidy across auctions.
        self._lot_cache: dict[str, dict[str, Any]] = {}
        self._cf_cookie_set: bool = False

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
        self._cf_cookie_set = False

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "HibidSource used outside `async with` — wrap in context manager",
            )
        return self._client

    async def _bootstrap_cf(self) -> None:
        """Hit a HiBid page once so Cloudflare sets `__cf_bm` in our cookie
        jar. Subsequent GraphQL POSTs ride that cookie; without it CF returns
        a 403 challenge page.
        """
        if self._cf_cookie_set:
            return
        try:
            await self._http.get(_BOOTSTRAP_URL)
        except Exception as exc:
            _log.warning("hibid CF bootstrap failed", error=str(exc))
            return
        self._cf_cookie_set = True

    async def _graphql(
        self, operation: str, variables: dict[str, Any], query: str,
    ) -> dict[str, Any]:
        await self._bootstrap_cf()
        body = json.dumps(
            {"operationName": operation, "variables": variables, "query": query},
        )
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://hibid.com",
            "referer": "https://hibid.com/",
            # Mimic the real PWA bundle's identification headers — reduces
            # Cloudflare heuristic-block risk and makes HiBid's server logs
            # correlate to us cleanly if they investigate traffic.
            "apollographql-client-name": "hibid-web",
            "apollographql-client-version": "1.19.1.1",
        }
        resp = await self._http.post(_GRAPHQL_URL, headers=headers, content=body)
        resp.raise_for_status()
        # Cloudflare returns an HTML challenge page (status 200 with HTML body)
        # when it suspects automated traffic. Detect explicitly so callers can
        # back off instead of crashing on JSONDecodeError.
        if resp.text.lstrip().startswith("<"):
            raise _CFChallenge(f"Cloudflare challenge response on {operation}")
        data: dict[str, Any] = resp.json()
        # GraphQL allows partial responses (`data` populated AND `errors`
        # non-empty). Log them — surfacing this in production logs is what
        # tells us when HiBid renames a field or returns a per-field error.
        errors = data.get("errors")
        if errors:
            _log.warning(
                "graphql partial errors", operation=operation, errors=errors,
            )
        return data

    async def discover_auctions(self) -> AsyncIterator[AuctionRef]:
        seen: set[str] = set()
        for i, province in enumerate(self.provinces):
            url = province_vehicles_url(province)
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
            except Exception as exc:
                _log.warning(
                    "discover_auctions province failed",
                    province=province, url=url, error=str(exc),
                )
                continue
            for auction_id in discover_auction_ids(resp.text):
                if auction_id in seen:
                    continue
                seen.add(auction_id)
                yield AuctionRef(
                    source="hibid",
                    source_auction_id=auction_id,
                    url=catalog_url(auction_id),
                )
            if i < len(self.provinces) - 1:
                await jittered_sleep()

    async def discover_vehicle_lots(
        self,
        province: str,
        *,
        category: int = _VEHICLE_CATEGORY_ID,
        status: str = "OPEN",
        sort_order: str = "NEWLY_ADDED",
    ) -> AsyncIterator[tuple[RawAuction, RawLot]]:
        """Walk cross-auction lot search filtered by (province, category).

        Yields ``(RawAuction, RawLot)`` tuples — the auction is derived from
        each lot's embedded ``auction { ... }`` sub-selection so the caller
        can upsert both rows in one transaction without a separate
        AuctionDetails round-trip.

        This is the canonical ingest path post-SPA: server-side category
        filtering means we only see vehicle lots; the catalog page's "this
        auction has 79 lots but only 3 are vehicles" over-fetch is gone.
        """
        page = 1
        while True:
            data = await self._graphql(
                "LotSearch",
                {
                    "auctionId": None,
                    "category": category,
                    "state": province,
                    "status": status,
                    "sortOrder": sort_order,
                    "filter": "ALL",
                    "isArchive": False,
                    "countAsView": False,
                    "hideGoogle": False,
                    "shippingOffered": False,
                    "pageNumber": page,
                    "pageLength": _PAGE_SIZE,
                },
                _LOT_SEARCH_FULL_QUERY,
            )
            paged = (
                ((data.get("data") or {}).get("lotSearch") or {})
                .get("pagedResults", {})
            )
            results_raw = paged.get("results") or []
            filtered = int(paged.get("filteredCount") or 0)
            results: list[dict[str, Any]] = [
                r for r in results_raw if isinstance(r, dict)  # type: ignore[reportUnknownVariableType]
            ]
            for rec in results:
                pair = _lot_record_to_raw_pair(rec)
                if pair is None:
                    continue
                yield pair
            if len(results) < _PAGE_SIZE or page * _PAGE_SIZE >= filtered:
                break
            page += 1

    async def fetch_auction(self, ref: AuctionRef) -> RawAuction:
        data = await self._graphql(
            "AuctionDetails",
            {"id": int(ref.source_auction_id), "countAsView": False},
            _AUCTION_DETAILS_QUERY,
        )
        det = parse_auction_details_response(data)
        return RawAuction(
            ref=ref,
            title=det.event_name,
            description=det.description,
            auctioneer_name=det.auctioneer_name,
            auctioneer_external_id=det.auctioneer_external_id,
            scheduled_start_at=det.bid_open_at,
            scheduled_end_at=det.bid_close_at,
            pickup_address=det.event_address,
            pickup_city=det.event_city,
            pickup_province=det.event_state,
            pickup_window_text=None,
            buyer_premium_pct=det.buyer_premium_pct or Decimal("0.10"),
            online_bidding_fee_pct=None,
            terms_text=None,
            auction_subtype="estate",
        )

    async def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]:
        self._lot_cache.clear()
        auction_id = int(ref.source_auction_id)
        page = 1
        while True:
            data = await self._graphql(
                "LotSearchLotOnly",
                {
                    "auctionId": auction_id,
                    "pageNumber": page,
                    "pageLength": _PAGE_SIZE,
                    "status": "ALL",
                    "sortOrder": "LOT_NUMBER",
                    "filter": "ALL",
                    "isArchive": False,
                    "countAsView": False,
                    "hideGoogle": False,
                },
                _LOT_SEARCH_QUERY,
            )
            paged = (
                ((data.get("data") or {}).get("lotSearch") or {})
                .get("pagedResults", {})
            )
            results_raw = paged.get("results") or []
            total = int(paged.get("totalCount") or 0)
            results: list[dict[str, Any]] = [
                r for r in results_raw if isinstance(r, dict)  # type: ignore[reportUnknownVariableType]
            ]
            for rec in results:
                item_id = rec.get("itemId")
                if not item_id:
                    continue
                source_lot_id = str(item_id)
                self._lot_cache[source_lot_id] = rec
                yield LotRef(
                    source="hibid",
                    source_auction_id=ref.source_auction_id,
                    source_lot_id=source_lot_id,
                    url=lot_url(str(rec.get("id") or item_id)),
                )
            if len(results) < _PAGE_SIZE or page * _PAGE_SIZE >= total:
                break
            page += 1

    async def fetch_lot(self, ref: LotRef) -> RawLot:
        rec = self._lot_cache.get(ref.source_lot_id)
        if rec is None:
            # Cache miss — single-lot fetch via GraphQL.
            rec = await self._fetch_one_lot_record(ref)
        summary = _summary_with_caller_fields(
            parse_lot_record(rec),
            ref=ref,
        )
        return RawLot(
            ref=ref,
            lot_number=summary.lot_number,
            title=summary.title,
            description=summary.description,
            photos=summary.photos,
            year=None,
            make=None,
            model=None,
            current_high_bid_cad=summary.current_high_bid_cad,
            bid_count_visible=summary.bid_count_visible,
            scheduled_end_at=None,
            lot_status="closed" if lot_is_closed(rec) else "open",
            extra=summary.extra,
        )

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        try:
            rec = await self._fetch_one_lot_record(ref)
        except _LotMissing:
            return BidObservation(
                ref=ref,
                observed_at=datetime.now(UTC),
                current_high_bid_cad=None,
                end_time_at_observation=None,
                status_at_observation="missing",
            )
        lot_state_raw = rec.get("lotState") or {}
        lot_state: dict[str, Any] = (
            lot_state_raw if isinstance(lot_state_raw, dict) else {}
        )
        high_bid = lot_state.get("highBid")
        bid_decimal: Decimal | None = None
        if isinstance(high_bid, (int, float)) and high_bid:
            bid_decimal = Decimal(str(high_bid))
        return BidObservation(
            ref=ref,
            observed_at=datetime.now(UTC),
            current_high_bid_cad=bid_decimal,
            end_time_at_observation=None,
            status_at_observation=(
                "closed" if lot_state.get("isClosed") else "open"
            ),
        )

    async def _fetch_one_lot_record(self, ref: LotRef) -> dict[str, Any]:
        """GraphQL fetch of a single lot by itemId; raises _LotMissing if gone."""
        data = await self._graphql(
            "LotSearchLotOnly",
            {
                "auctionId": int(ref.source_auction_id),
                "pageNumber": 1,
                "pageLength": 1,
                "eventItemIds": [int(ref.source_lot_id)],
                "status": "ALL",
                "sortOrder": "LOT_NUMBER",
                "filter": "ALL",
                "isArchive": False,
                "countAsView": False,
                "hideGoogle": False,
            },
            _LOT_SEARCH_QUERY,
        )
        results_raw = (
            ((data.get("data") or {}).get("lotSearch") or {})
            .get("pagedResults", {})
            .get("results", [])
        )
        results: list[dict[str, Any]] = [
            r for r in results_raw if isinstance(r, dict)  # type: ignore[reportUnknownVariableType]
        ]
        if not results:
            raise _LotMissing(ref.source_lot_id)
        return results[0]


class _LotMissing(Exception):
    """Internal signal: lot not present in GraphQL response."""


class _CFChallenge(Exception):
    """Cloudflare returned an HTML challenge page instead of JSON. Callers
    should back off (exponential or worker-level) rather than retry tightly."""


def _summary_with_caller_fields(
    s: HibidLotSummary, *, ref: LotRef,
) -> HibidLotSummary:
    """Backfill auction_external_id + url from the caller's LotRef."""
    s.auction_external_id = ref.source_auction_id
    s.url = ref.url
    return s


def _lot_record_to_raw_pair(
    rec: dict[str, Any],
) -> tuple[RawAuction, RawLot] | None:
    """Convert one cross-auction LotSearch record into (RawAuction, RawLot).

    Returns None if the record is missing the auction sub-selection or the
    lot itemId — both are required for upsert.
    """
    auction_raw = rec.get("auction") or {}
    if not isinstance(auction_raw, dict) or not auction_raw.get("id"):
        return None
    item_id_raw = rec.get("itemId")
    if not item_id_raw:
        return None
    auction_id_str = str(auction_raw["id"])
    source_lot_id = str(item_id_raw)
    auctioneer_raw = auction_raw.get("auctioneer") or {}
    auctioneer: dict[str, Any] = (
        auctioneer_raw if isinstance(auctioneer_raw, dict) else {}
    )
    auction_ref = AuctionRef(
        source="hibid",
        source_auction_id=auction_id_str,
        url=catalog_url(auction_id_str),
    )
    raw_auction = RawAuction(
        ref=auction_ref,
        title=auction_raw.get("eventName"),
        description=None,
        auctioneer_name=auctioneer.get("name"),
        auctioneer_external_id=(
            str(auctioneer["id"]) if auctioneer.get("id") else None
        ),
        scheduled_start_at=_parse_iso(auction_raw.get("bidOpenDateTime")),
        scheduled_end_at=_parse_iso(auction_raw.get("bidCloseDateTime")),
        pickup_address=auction_raw.get("eventAddress"),
        pickup_city=auction_raw.get("eventCity"),
        pickup_province=auction_raw.get("eventState"),
        pickup_window_text=None,
        buyer_premium_pct=_to_decimal(auction_raw.get("buyerPremiumRate"))
        or Decimal("0.10"),
        online_bidding_fee_pct=None,
        terms_text=None,
        auction_subtype="estate",
    )
    summary = parse_lot_record(rec)
    summary.auction_external_id = auction_id_str
    summary.url = lot_url(str(rec.get("id") or item_id_raw))
    lot_ref = LotRef(
        source="hibid",
        source_auction_id=auction_id_str,
        source_lot_id=source_lot_id,
        url=summary.url,
    )
    raw_lot = RawLot(
        ref=lot_ref,
        lot_number=summary.lot_number,
        title=summary.title,
        description=summary.description,
        photos=summary.photos,
        year=None,
        make=None,
        model=None,
        current_high_bid_cad=summary.current_high_bid_cad,
        bid_count_visible=summary.bid_count_visible,
        scheduled_end_at=raw_auction.scheduled_end_at,
        lot_status="closed" if lot_is_closed(rec) else "open",
        extra=summary.extra,
    )
    return raw_auction, raw_lot


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.strip().rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value)) if value else None
    return None


# Register at import time so the lot-scraper / discoverer worker / dashboard
# health view can enumerate covered platforms via SOURCES (Phase 0 design #11).
register(HibidSource(provinces=list(settings.hibid_provinces)))
