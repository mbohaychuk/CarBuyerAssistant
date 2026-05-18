"""Postgres UPSERT helpers for Auction + AuctionLot rows.

These wrap `INSERT ... ON CONFLICT DO UPDATE` with the project's coalesce-on-
update + content-cascade semantics. Originally lived inside the legacy
`auction_discoverer` and `lot_scraper` worker apps; relocated here once the
ingester app became the sole caller and the workers were retired.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.sources.base import RawAuction, RawLot
from carbuyer.sources.resolver import canonicalize_url


async def upsert_auction(
    session: AsyncSession,
    raw: RawAuction,
    *,
    discovered_via: str,
) -> Auction:
    """Atomic UPSERT keyed on (source, source_auction_id).

    On conflict: refreshes last_seen_at, copies non-None fields from `raw` (never
    overwrites with None via ``coalesce(EXCLUDED, table)``), and appends
    `discovered_via` to the array if not already present.
    """
    now = datetime.now(UTC)
    canonical = canonicalize_url(raw.ref.url)

    insert_values: dict[str, object] = {
        "source": raw.ref.source,
        "source_auction_id": raw.ref.source_auction_id,
        "url": raw.ref.url,
        "canonical_url": canonical,
        "discovered_via": [discovered_via],
        "auction_subtype": raw.auction_subtype,
        "auctioneer_name": raw.auctioneer_name,
        "auctioneer_external_id": raw.auctioneer_external_id,
        "title": raw.title,
        "description": raw.description,
        "terms_text": raw.terms_text,
        "scheduled_start_at": raw.scheduled_start_at,
        "scheduled_end_at": raw.scheduled_end_at,
        "pickup_address": raw.pickup_address,
        "pickup_city": raw.pickup_city,
        "pickup_province": raw.pickup_province,
        "pickup_window_text": raw.pickup_window_text,
        "buyer_premium_pct": raw.buyer_premium_pct,
        "buyer_premium_max_cad": raw.buyer_premium_max_cad,
        "buyer_premium_min_cad": raw.buyer_premium_min_cad,
        "online_bidding_fee_pct": raw.online_bidding_fee_pct,
        "status": "upcoming",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    stmt = pg_insert(Auction).values(**insert_values)
    excluded = stmt.excluded
    update_set: dict[str, object] = {
        "url": excluded.url,
        "canonical_url": excluded.canonical_url,
        "auction_subtype": func.coalesce(
            excluded.auction_subtype, Auction.auction_subtype,
        ),
        "auctioneer_name": func.coalesce(
            excluded.auctioneer_name, Auction.auctioneer_name,
        ),
        "auctioneer_external_id": func.coalesce(
            excluded.auctioneer_external_id, Auction.auctioneer_external_id,
        ),
        "title": func.coalesce(excluded.title, Auction.title),
        "description": func.coalesce(excluded.description, Auction.description),
        "terms_text": func.coalesce(excluded.terms_text, Auction.terms_text),
        "scheduled_start_at": func.coalesce(
            excluded.scheduled_start_at, Auction.scheduled_start_at,
        ),
        "scheduled_end_at": func.coalesce(
            excluded.scheduled_end_at, Auction.scheduled_end_at,
        ),
        "pickup_address": func.coalesce(
            excluded.pickup_address, Auction.pickup_address,
        ),
        "pickup_city": func.coalesce(excluded.pickup_city, Auction.pickup_city),
        "pickup_province": func.coalesce(
            excluded.pickup_province, Auction.pickup_province,
        ),
        "pickup_window_text": func.coalesce(
            excluded.pickup_window_text, Auction.pickup_window_text,
        ),
        "buyer_premium_pct": func.coalesce(
            excluded.buyer_premium_pct, Auction.buyer_premium_pct,
        ),
        "buyer_premium_max_cad": func.coalesce(
            excluded.buyer_premium_max_cad, Auction.buyer_premium_max_cad,
        ),
        "buyer_premium_min_cad": func.coalesce(
            excluded.buyer_premium_min_cad, Auction.buyer_premium_min_cad,
        ),
        "online_bidding_fee_pct": func.coalesce(
            excluded.online_bidding_fee_pct, Auction.online_bidding_fee_pct,
        ),
        "last_seen_at": excluded.last_seen_at,
        "updated_at": func.now(),  # ORM onupdate doesn't fire on ON CONFLICT
        # Atomic dedup-append: array || EXCLUDED.array, then DISTINCT.
        "discovered_via": text(
            "ARRAY(SELECT DISTINCT unnest("
            "auctions.discovered_via || EXCLUDED.discovered_via))",
        ),
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "source_auction_id"],
        set_=update_set,
    ).returning(Auction)
    # populate_existing=True so RETURNING overrides any stale instance in the
    # session's identity map (otherwise an UPSERT update wouldn't refresh the
    # earlier-loaded ORM object's attributes).
    result = await session.execute(stmt, execution_options={"populate_existing": True})
    return result.scalar_one()


# Fields that, when changed, invalidate downstream worker output.
# Bid columns (current_high_bid_cad, bid_count_visible, lot_status, …) are
# deliberately NOT here — they're the bid-poller's domain.
_CONTENT_TRIGGER_FIELDS: tuple[str, ...] = (
    "title", "description", "photos",
    "year", "make", "model", "vin", "mileage_km",
)


async def _upsert_lot(
    session: AsyncSession,
    auction_id: int,
    raw: RawLot,
    *,
    parser_version: str,
) -> AuctionLot:
    """Atomic UPSERT on (auction_id, source_lot_id) with coalesce-on-update.

    Caller wraps in upsert_lot_with_status_cascade to apply the trigger-field
    cascade. This inner function is the pure SQL operation.

    Note: ``RawLot.extra`` is not persisted to ``AuctionLot`` (no column for it
    by design). It flows in-memory from scraper to enricher (Phase 3) so
    source-specific fields like Carfax URLs / reserve status / buy-now price
    can be promoted to canonical columns once 2+ sources surface the same key.
    """
    insert_values: dict[str, object] = {
        "auction_id": auction_id,
        "source_lot_id": raw.ref.source_lot_id,
        "source_lot_row_id": raw.source_lot_row_id,
        "lot_number": raw.lot_number,
        "url": raw.ref.url,
        "parser_version": parser_version,
        "title": raw.title,
        "description": raw.description,
        "photos": raw.photos,
        "year": raw.year,
        "make": raw.make,
        "model": raw.model,
        "trim": raw.trim,
        "mileage_km": raw.mileage_km,
        "vin": raw.vin,
        "lot_status": raw.lot_status,
        "scheduled_end_at": raw.scheduled_end_at,
    }
    stmt = pg_insert(AuctionLot).values(**insert_values)
    excluded = stmt.excluded

    # Per Phase 0 column-ownership: lot-scraper does NOT write bid columns.
    # `lot_status` belongs to the bid-poller after initial INSERT — leaving it
    # out of `update_values` means lot-scraper sets the initial 'open' on
    # creation, then never touches the column again. Bid-poller advances it
    # to 'closing_soon' / 'extended' / 'closed' / 'sold' / 'unsold'.
    #
    # Per Phase 3 design overlay #5: year/make/model/trim/vin/mileage_km are
    # written only on INSERT. After enrichment normalizes "F150" → "F-150",
    # a rescrape's raw "F150" must NOT clobber the normalized value (a
    # coalesce(EXCLUDED.model, lot.model) here produces enrich → rescrape-
    # clobber → re-enrich flap that burns OpenAI budget). They remain in
    # _CONTENT_TRIGGER_FIELDS for cascade detection (snapshots match → cascade
    # correctly does not fire).
    update_values: dict[str, object] = {
        "url": excluded.url,
        "lot_number": func.coalesce(excluded.lot_number, AuctionLot.lot_number),
        "parser_version": excluded.parser_version,
        # source_lot_row_id can change when HiBid re-lists the same itemId
        # in a new auction event. Always take the latest so bid_poller can
        # look up the current row id.
        "source_lot_row_id": excluded.source_lot_row_id,
        # scheduled_end_at refreshes on rescrape so soft-close-style end-time
        # updates (McDougall sometimes nudges per-lot close times when the
        # auction-event reshuffles) propagate to bid_poller's priority queue.
        # Coalesce so a transient NULL on rescrape doesn't clobber a known
        # end time.
        "scheduled_end_at": func.coalesce(
            excluded.scheduled_end_at, AuctionLot.scheduled_end_at,
        ),
        "updated_at": func.now(),
    }
    for field_name in ("title", "description", "photos"):
        update_values[field_name] = func.coalesce(
            getattr(excluded, field_name), getattr(AuctionLot, field_name),
        )
    stmt = stmt.on_conflict_do_update(
        index_elements=["auction_id", "source_lot_id"],
        set_=update_values,
    ).returning(AuctionLot)
    result = await session.execute(
        stmt, execution_options={"populate_existing": True},
    )
    return result.scalar_one()


async def upsert_lot_with_status_cascade(
    session: AsyncSession,
    auction_id: int,
    raw: RawLot,
    *,
    parser_version: str,
) -> AuctionLot:
    """UPSERT a lot; reset all four pipeline statuses if any content trigger
    field or parser_version changed. Idempotent re-scrapes preserve status.
    """
    pre = (
        await session.execute(
            select(AuctionLot).where(
                AuctionLot.auction_id == auction_id,
                AuctionLot.source_lot_id == raw.ref.source_lot_id,
            ),
        )
    ).scalar_one_or_none()
    if pre is not None:
        pre_snapshot: dict[str, object] | None = {
            f: getattr(pre, f) for f in _CONTENT_TRIGGER_FIELDS
        }
        pre_parser_version: str | None = pre.parser_version
    else:
        pre_snapshot = None
        pre_parser_version = None

    lot = await _upsert_lot(session, auction_id, raw, parser_version=parser_version)

    if pre_snapshot is None:
        # Fresh insert — server defaults already set all statuses to PENDING.
        return lot

    post_snapshot = {f: getattr(lot, f) for f in _CONTENT_TRIGGER_FIELDS}
    content_changed = post_snapshot != pre_snapshot
    parser_changed = lot.parser_version != pre_parser_version
    if content_changed or parser_changed:
        lot.enrichment_status = EnrichmentStatus.PENDING
        lot.valuation_status = ValuationStatus.PENDING
        # vision_status='skipped' is set by the Phase 8 vision-batcher when a
        # lot has no photos OR every photo download/decode failed. Sub-threshold
        # lots stay PENDING and are simply not selected by the batcher's
        # shortlist that night — they re-enter the shortlist automatically if a
        # bid update lifts their price_deal_score above the threshold. Don't
        # promote SKIPPED → PENDING on a content rescrape: SKIPPED means
        # "no usable photos", and a rescrape doesn't add photos that weren't
        # there before.
        if lot.vision_status != VisionStatus.SKIPPED:
            lot.vision_status = VisionStatus.PENDING
        lot.notification_status = NotificationStatus.PENDING
        await session.flush()
    return lot
