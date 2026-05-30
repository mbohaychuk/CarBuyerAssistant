"""Postgres UPSERT for PrivateListing rows.

Mirrors upserts.upsert_lot_with_status_cascade semantics:
- idempotent ON CONFLICT DO UPDATE keyed on (source, source_listing_id)
- coalesce-non-null: never overwrites an existing value with NULL
- always bumps last_seen_at
- resets enrichment_status / valuation_status to 'pending' when content
  trigger fields (title / description / photos / ask_price_cad) change or
  on a fresh insert
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import EnrichmentStatus, ValuationStatus
from carbuyer.db.models import PrivateListing
from carbuyer.sources.base import RawPrivateListing
from carbuyer.sources.resolver import canonicalize_url

# Fields whose change invalidates enrichment + valuation outputs.
# Narrower than the auction upsert's trigger set: year/make/model/vin/mileage_km
# are NOT included because the enricher is the canonical source for those on a
# private listing (the scraper's raw values are best-effort hints). Only a
# change to the human-authored content (title/description/photos) or the ask
# price warrants re-enriching/re-valuing.
_CONTENT_TRIGGER_FIELDS: tuple[str, ...] = (
    "title", "description", "photos", "ask_price_cad",
)


async def upsert_private_listing(
    session: AsyncSession,
    raw: RawPrivateListing,
) -> PrivateListing:
    """Atomic UPSERT on (source, source_listing_id).

    Returns the post-upsert ORM instance. Resets enrichment_status /
    valuation_status to 'pending' when any content trigger field changed
    (or on a fresh insert). Idempotent re-upserts with identical content
    preserve existing statuses.
    """
    # Capture BEFORE the upsert — the returned row is the same identity-map object.
    # Pre-fetch so we can snapshot content trigger fields before the upsert
    # overwrites them. None means this is a fresh insert.
    pre = (
        await session.execute(
            select(PrivateListing).where(
                PrivateListing.source == raw.source,
                PrivateListing.source_listing_id == raw.source_listing_id,
            ),
        )
    ).scalar_one_or_none()
    pre_snapshot: dict[str, object] | None = (
        {f: getattr(pre, f) for f in _CONTENT_TRIGGER_FIELDS} if pre is not None else None
    )

    canonical = canonicalize_url(raw.url)

    insert_values: dict[str, object] = {
        "source": raw.source,
        "source_listing_id": raw.source_listing_id,
        "url": raw.url,
        "canonical_url": canonical,
        "title": raw.title,
        "description": raw.description,
        "photos": raw.photos,
        "year": raw.year,
        "make": raw.make,
        "model": raw.model,
        "trim": raw.trim,
        "mileage_km": raw.mileage_km,
        "vin": raw.vin,
        "ask_price_cad": raw.ask_price_cad,
        "pickup_province": raw.pickup_province,
        "pickup_city": raw.pickup_city,
        "first_seen_at": func.now(),
        "last_seen_at": func.now(),
    }

    stmt = pg_insert(PrivateListing).values(**insert_values)
    excluded = stmt.excluded

    update_set: dict[str, object] = {
        "url": excluded.url,
        "canonical_url": excluded.canonical_url,
        # Coalesce: never clobber an existing non-null value with a null rescrape.
        "title": func.coalesce(excluded.title, PrivateListing.title),
        "description": func.coalesce(excluded.description, PrivateListing.description),
        # coalesce protects against NULL only, not empty arrays — a []-photos
        # re-scrape clobbers existing photos (treated as a parser failure upstream).
        "photos": func.coalesce(excluded.photos, PrivateListing.photos),
        "year": func.coalesce(excluded.year, PrivateListing.year),
        "make": func.coalesce(excluded.make, PrivateListing.make),
        "model": func.coalesce(excluded.model, PrivateListing.model),
        "trim": func.coalesce(excluded.trim, PrivateListing.trim),
        "mileage_km": func.coalesce(excluded.mileage_km, PrivateListing.mileage_km),
        "vin": func.coalesce(excluded.vin, PrivateListing.vin),
        "ask_price_cad": func.coalesce(excluded.ask_price_cad, PrivateListing.ask_price_cad),
        "pickup_province": func.coalesce(excluded.pickup_province, PrivateListing.pickup_province),
        "pickup_city": func.coalesce(excluded.pickup_city, PrivateListing.pickup_city),
        # Always stamp the most recent scrape time.
        "last_seen_at": func.now(),
        "updated_at": func.now(),
    }

    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "source_listing_id"],
        set_=update_set,
    ).returning(PrivateListing)

    result = await session.execute(stmt, execution_options={"populate_existing": True})
    listing = result.scalar_one()

    if pre_snapshot is None:
        # Fresh insert: server defaults already set statuses to 'pending'.
        return listing

    post_snapshot = {f: getattr(listing, f) for f in _CONTENT_TRIGGER_FIELDS}
    if post_snapshot != pre_snapshot:
        listing.enrichment_status = EnrichmentStatus.PENDING
        listing.valuation_status = ValuationStatus.PENDING
        await session.flush()

    return listing
