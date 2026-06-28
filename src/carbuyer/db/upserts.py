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
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, VehicleOffer
from carbuyer.sources.base import RawAuction, RawListing, RawLot
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


async def upsert_lot_with_status_cascade(
    session: AsyncSession,
    auction_id: int,
    raw: RawLot,
    *,
    parser_version: str,
) -> AuctionLot:
    """UPSERT an auction lot (vehicle_offer parent + auction_lot child) and
    reset all four pipeline statuses if any content trigger field or
    parser_version changed. Idempotent re-scrapes preserve status.

    The offer split makes this a two-table write, so we drive it through the
    ORM unit-of-work (which routes parent vs child columns automatically) off
    the find-by-natural-key SELECT rather than a single ON CONFLICT. The
    ingester is single-instance (advisory lock), so no concurrent INSERT of the
    same (auction_id, source_lot_id) can race the find→write window.

    Note: ``RawLot.extra`` is not persisted (no column for it by design). It
    flows in-memory from scraper to enricher so source-specific fields like
    Carfax URLs / reserve status / buy-now price can be promoted to canonical
    columns once 2+ sources surface the same key.
    """
    existing = (
        await session.execute(
            select(AuctionLot).where(
                AuctionLot.auction_id == auction_id,
                AuctionLot.source_lot_id == raw.ref.source_lot_id,
            ),
        )
    ).scalar_one_or_none()

    if existing is None:
        # Fresh insert — year/make/model/trim/vin/mileage_km are written ONLY
        # here so a later rescrape's raw heuristic value can't clobber the
        # enricher's normalization. Server defaults set all statuses to PENDING.
        lot = AuctionLot(
            auction_id=auction_id,
            source_lot_id=raw.ref.source_lot_id,
            source_lot_row_id=raw.source_lot_row_id,
            lot_number=raw.lot_number,
            url=raw.ref.url,
            parser_version=parser_version,
            title=raw.title,
            description=raw.description,
            photos=raw.photos,
            year=raw.year,
            make=raw.make,
            model=raw.model,
            trim=raw.trim,
            mileage_km=raw.mileage_km,
            vin=raw.vin,
            lot_status=raw.lot_status,
            scheduled_end_at=raw.scheduled_end_at,
        )
        session.add(lot)
        await session.flush()
        return lot

    pre_snapshot = {f: getattr(existing, f) for f in _CONTENT_TRIGGER_FIELDS}
    pre_parser_version = existing.parser_version

    # Coalesce-on-update: never overwrite a known value with None.
    existing.url = raw.ref.url
    existing.parser_version = parser_version
    # source_lot_row_id can change when HiBid re-lists the same itemId in a new
    # auction event. Always take the latest so bid_poller can look it up.
    existing.source_lot_row_id = raw.source_lot_row_id
    if raw.lot_number is not None:
        existing.lot_number = raw.lot_number
    # scheduled_end_at refreshes on rescrape (McDougall nudges per-lot close
    # times) but a transient NULL must not clobber a known end time.
    if raw.scheduled_end_at is not None:
        existing.scheduled_end_at = raw.scheduled_end_at
    for field_name in ("title", "description", "photos"):
        value = getattr(raw, field_name)
        if value is not None:
            setattr(existing, field_name, value)
    # year/make/model/trim/vin/mileage_km: write-once (see INSERT path).
    # lot_status: bid-poller owns it after INSERT — never re-scraped here.
    existing.updated_at = func.now()  # ORM onupdate doesn't fire on no-op flush
    await session.flush()
    await _apply_content_cascade(session, existing, pre_snapshot, pre_parser_version)
    return existing


async def _apply_content_cascade(
    session: AsyncSession,
    offer: VehicleOffer,
    pre_snapshot: dict[str, object],
    pre_parser_version: str | None,
) -> None:
    """Reset the four pipeline statuses to PENDING when a content trigger field
    or parser_version changed since the pre-write snapshot. Shared by the auction
    and listing upserts (the trigger fields all live on the offer parent).
    """
    post_snapshot = {f: getattr(offer, f) for f in _CONTENT_TRIGGER_FIELDS}
    if post_snapshot == pre_snapshot and offer.parser_version == pre_parser_version:
        return
    offer.enrichment_status = EnrichmentStatus.PENDING
    offer.valuation_status = ValuationStatus.PENDING
    # Don't promote SKIPPED → PENDING on a content rescrape: SKIPPED means "no
    # usable photos", and a rescrape doesn't add photos that weren't there
    # before. Sub-threshold offers stay PENDING and simply aren't selected by
    # the vision-batcher's nightly shortlist.
    if offer.vision_status != VisionStatus.SKIPPED:
        offer.vision_status = VisionStatus.PENDING
    offer.notification_status = NotificationStatus.PENDING
    await session.flush()


async def upsert_private_listing(
    session: AsyncSession,
    raw: RawListing,
    *,
    parser_version: str,
) -> PrivateListing:
    """UPSERT a private listing (vehicle_offer parent + private_listing child),
    keyed on the natural key (source, source_listing_id). Mirrors the auction
    lot upsert: a two-table ORM write driven off a find-by-key SELECT (the
    ingester is single-instance, so no concurrent INSERT can race), with the
    shared content-change status cascade.

    Listing economics (asking price, days-on-market, status) refresh every
    ingest so price drops propagate; only the vehicle content fields gate the
    re-enrichment cascade. Photos are source URLs for deep-linking only — never
    rehosted; seller PII is never persisted.
    """
    existing = (
        await session.execute(
            select(PrivateListing).where(
                PrivateListing.source == raw.ref.source,
                PrivateListing.source_listing_id == raw.ref.source_listing_id,
            ),
        )
    ).scalar_one_or_none()

    if existing is None:
        listing = PrivateListing(
            source=raw.ref.source,
            source_listing_id=raw.ref.source_listing_id,
            url=raw.ref.url,
            parser_version=parser_version,
            title=raw.title,
            description=raw.description,
            photos=raw.photos,
            year=raw.year,
            make=raw.make,
            model=raw.model,
            trim=raw.trim,
            mileage_km=raw.mileage_km,
            vin=raw.vin,
            location_province=raw.location_province,
            asking_price_cad=raw.asking_price_cad,
            seller_type=raw.seller_type,
            days_on_market=raw.days_on_market,
            listing_status=raw.listing_status,
            first_seen_at=raw.first_seen_at or datetime.now(UTC),
        )
        session.add(listing)
        await session.flush()
        return listing

    pre_snapshot = {f: getattr(existing, f) for f in _CONTENT_TRIGGER_FIELDS}
    pre_parser_version = existing.parser_version
    pre_asking = existing.asking_price_cad

    existing.url = raw.ref.url
    existing.parser_version = parser_version
    for field_name in ("title", "description", "photos"):
        value = getattr(raw, field_name)
        if value is not None:
            setattr(existing, field_name, value)
    # year/make/model/trim/vin/mileage_km: write-once (enricher normalizes them).
    # Listing economics refresh on every ingest.
    if raw.asking_price_cad is not None:
        existing.asking_price_cad = raw.asking_price_cad
    if raw.days_on_market is not None:
        existing.days_on_market = raw.days_on_market
    if raw.seller_type is not None:
        existing.seller_type = raw.seller_type
    if raw.location_province is not None:
        existing.location_province = raw.location_province
    existing.listing_status = raw.listing_status
    existing.updated_at = func.now()
    await session.flush()
    await _apply_content_cascade(session, existing, pre_snapshot, pre_parser_version)
    # A price change with no content change must still re-value (the deal score
    # + want matching are price-dependent — a drop into a want's budget has to
    # re-fire). Re-pend valuation only, not enrichment (make/model unchanged).
    if (
        existing.asking_price_cad != pre_asking
        and existing.valuation_status != ValuationStatus.PENDING
    ):
        existing.valuation_status = ValuationStatus.PENDING
        await session.flush()
    return existing
