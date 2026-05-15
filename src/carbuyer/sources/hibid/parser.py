"""HiBid response parsers (post-SPA migration, May 2026).

HiBid migrated from server-rendered HTML+lotModels to a SPA backed by a
single GraphQL endpoint at /graphql. Three response shapes matter:

  1. SSR'd auction LIST page   -> regex auction_id from <a href> patterns
  2. AuctionDetails GraphQL    -> auction metadata (auctioneer, dates, BP)
  3. LotSearchLotOnly GraphQL  -> paginated lots within an auction

The catalog page HTML is now empty of lot data; do not parse it for lots.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# Discovery list page links: <a href="/{province-slug}/auction/{id}/{slug}">.
# The SSR list page mixes auction cards with category links; we filter on
# the `/auction/{id}/` path segment so category nav doesn't sneak in.
_AUCTION_HREF = re.compile(
    r'href="/[a-z-]+/auction/(?P<id>\d+)/(?P<slug>[^"]+)"',
)


def discover_auction_ids(html: str) -> list[str]:
    """Extract unique auction IDs from a HiBid province list page."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _AUCTION_HREF.finditer(html):
        aid = m.group("id")
        if aid in seen:
            continue
        seen.add(aid)
        out.append(aid)
    return out


@dataclass(slots=True)
class HibidLotSummary:
    """Parsed shape of a single HiBid Lot record from LotSearchLotOnly."""

    source_lot_id: str          # itemId (stable event-item id)
    lot_number: str | None       # display number, e.g. "201"
    title: str | None            # `lead`
    description: str | None
    # year/make/model are NOT separate fields in HiBid's API; the description
    # enricher (Phase 3) parses them out of the title text downstream.
    year: int | None
    make: str | None
    model: str | None
    current_high_bid_cad: Decimal | None
    bid_count_visible: int | None
    photos: list[str]
    # Lot-level end times are not exposed by HiBid's lot query; the auction-
    # level bidCloseDateTime drives lot scheduling. Left as None here so
    # callers can backfill from AuctionDetails.
    end_at: datetime | None
    auction_external_id: str | None  # populated by source.py from caller scope
    url: str | None                   # populated by source.py via lot_url(id)
    extra: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]


@dataclass(slots=True)
class HibidAuctionDetails:
    """Parsed shape of the AuctionDetails GraphQL response."""

    auction_id: str
    event_name: str | None
    description: str | None
    bid_open_at: datetime | None
    bid_close_at: datetime | None
    event_address: str | None
    event_city: str | None
    event_state: str | None       # province code in CA
    buyer_premium_pct: Decimal | None
    auctioneer_name: str | None
    auctioneer_external_id: str | None


def parse_lot_search_response(data: dict[str, Any]) -> list[HibidLotSummary]:
    """Parse a LotSearchLotOnly GraphQL response into HibidLotSummary entries."""
    results = (
        ((data.get("data") or {}).get("lotSearch") or {})
        .get("pagedResults", {})
        .get("results", [])
    )
    out: list[HibidLotSummary] = []
    for rec in results:
        if not isinstance(rec, dict):
            continue
        if not rec.get("itemId"):
            continue
        out.append(parse_lot_record(rec))  # type: ignore[reportUnknownArgumentType]
    return out


def parse_lot_record(rec: dict[str, Any]) -> HibidLotSummary:
    """Parse one Lot record from the GraphQL response."""
    lot_state_raw = rec.get("lotState") or {}
    lot_state: dict[str, Any] = lot_state_raw if isinstance(lot_state_raw, dict) else {}
    pictures_raw = rec.get("pictures") or []
    photos: list[str] = []
    if isinstance(pictures_raw, list):
        for p in pictures_raw:  # type: ignore[reportUnknownVariableType]
            if not isinstance(p, dict):
                continue
            entry: dict[str, Any] = p  # type: ignore[reportUnknownVariableType]
            url_val = entry.get("fullSizeLocation")
            if isinstance(url_val, str) and url_val:
                photos.append(url_val)
    return HibidLotSummary(
        source_lot_id=str(rec.get("itemId") or ""),
        lot_number=str(rec.get("lotNumber") or "") or None,
        title=rec.get("lead"),
        description=rec.get("description"),
        year=None,
        make=None,
        model=None,
        current_high_bid_cad=_to_decimal(lot_state.get("highBid")),
        bid_count_visible=_to_int(lot_state.get("bidCount")),
        photos=photos,
        end_at=None,
        auction_external_id=None,
        url=None,
        extra={
            "lotState": lot_state,
            "shippingOffered": rec.get("shippingOffered"),
            "category": rec.get("category"),
            "pictureCount": rec.get("pictureCount"),
            # HiBid emits two ids: row-level `id` and stable `itemId`. Keep
            # `lotId` (the row id) for log/debug correlation.
            "lotId": rec.get("id"),
        },
    )


def lot_is_closed(rec: dict[str, Any]) -> bool:
    """Whether a Lot record reports its bidding closed."""
    lot_state_raw = rec.get("lotState") or {}
    if not isinstance(lot_state_raw, dict):
        return False
    return bool(lot_state_raw.get("isClosed"))


def parse_auction_details_response(data: dict[str, Any]) -> HibidAuctionDetails:
    """Parse the AuctionDetails GraphQL response."""
    a_raw = (data.get("data") or {}).get("auction") or {}
    a: dict[str, Any] = a_raw if isinstance(a_raw, dict) else {}
    auctioneer_raw = a.get("auctioneer") or {}
    auctioneer: dict[str, Any] = (
        auctioneer_raw if isinstance(auctioneer_raw, dict) else {}
    )
    return HibidAuctionDetails(
        auction_id=str(a.get("id") or ""),
        event_name=a.get("eventName"),
        description=a.get("description"),
        bid_open_at=_parse_dt(a.get("bidOpenDateTime")),
        bid_close_at=_parse_dt(a.get("bidCloseDateTime")),
        event_address=a.get("eventAddress"),
        event_city=a.get("eventCity"),
        event_state=a.get("eventState"),
        buyer_premium_pct=_to_decimal(a.get("buyerPremiumRate")),
        auctioneer_name=auctioneer.get("name"),
        auctioneer_external_id=str(auctioneer.get("id") or "") or None,
    )


def _to_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):  # before int — bool subtypes int
        return None
    if isinstance(v, (int, float)):
        # Treat literal 0 as "no bid yet" so downstream doesn't show $0 bids.
        return Decimal(str(v)) if v != 0 else None
    if isinstance(v, str):
        cleaned = re.sub(r"[^\d.\-]", "", v)
        if not cleaned or cleaned in {"-", "."}:
            return None
        try:
            d = Decimal(cleaned)
        except InvalidOperation:
            return None
        return d if d != 0 else None
    return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _parse_dt(value: Any) -> datetime | None:
    """HiBid GraphQL emits ISO 8601; older paths sometimes emit ms-epoch."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # ms-epoch heuristic (any timestamp larger than this is in ms).
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        s = value.strip().rstrip("Z")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    return None
