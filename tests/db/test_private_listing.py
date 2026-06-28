"""S2 — private-listing write path: RawListing DTO + upsert_private_listing.

Proven with hand-built RawListings (as a ListingSource would yield); no real
scraping. Asserts the two-table write lands a vehicle_offer(offer_kind='private')
parent + a private_listing child under one id, keyed on (source,
source_listing_id), idempotent on re-ingest, with the status cascade firing on
content change.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import EnrichmentStatus, ListingStatus, ValuationStatus
from carbuyer.db.models import PrivateListing, VehicleOffer
from carbuyer.db.upserts import upsert_private_listing
from carbuyer.sources.base import ListingRef, ListingSource, RawListing, SourceType


def _raw_listing(asking: str = "15000", **over: object) -> RawListing:
    base: dict[str, object] = {
        "ref": ListingRef(source="kijiji", source_listing_id="K1", url="http://k/1"),
        "title": "2005 Lexus GX 470",
        "description": "clean, well maintained",
        "photos": ["http://k/1/p1.jpg"],
        "year": 2005, "make": "Lexus", "model": "GX 470",
        "asking_price_cad": Decimal(asking),
        "seller_type": "private",
        "days_on_market": 12,
        "listing_status": ListingStatus.ACTIVE.value,
    }
    base.update(over)
    return RawListing(**base)  # type: ignore[arg-type]


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def test_listing_source_kind() -> None:
    assert ListingSource.kind == "listing"
    assert "listing" in SourceType.__args__  # type: ignore[attr-defined]


async def test_upsert_creates_parent_and_child(session: AsyncSession) -> None:
    listing = await upsert_private_listing(session, _raw_listing(), parser_version="v1")
    await session.flush()
    lid = listing.id
    assert lid is not None
    assert listing.offer_kind == "private"

    session.expire_all()
    loaded = (
        await session.execute(select(VehicleOffer).where(VehicleOffer.id == lid))
    ).scalar_one()
    assert isinstance(loaded, PrivateListing)
    # Parent columns.
    assert loaded.make == "Lexus"
    assert loaded.url == "http://k/1"
    assert loaded.enrichment_status == EnrichmentStatus.PENDING
    # Child columns.
    assert loaded.asking_price_cad == Decimal("15000")
    assert loaded.source == "kijiji"
    assert loaded.source_listing_id == "K1"
    assert loaded.listing_status == ListingStatus.ACTIVE.value


async def test_upsert_is_idempotent_on_natural_key(session: AsyncSession) -> None:
    first = await upsert_private_listing(session, _raw_listing(asking="15000"), parser_version="v1")
    await session.flush()
    first_id = first.id
    # Re-ingest the same listing with a dropped price — updates, no duplicate.
    second = await upsert_private_listing(
        session, _raw_listing(asking="14000"), parser_version="v1",
    )
    await session.flush()
    assert second.id == first_id
    assert second.asking_price_cad == Decimal("14000")
    assert await _count(session, PrivateListing) == 1
    assert await _count(session, VehicleOffer) == 1


async def test_upsert_resets_statuses_on_content_change(session: AsyncSession) -> None:
    listing = await upsert_private_listing(session, _raw_listing(), parser_version="v1")
    listing.enrichment_status = EnrichmentStatus.DONE
    listing.valuation_status = ValuationStatus.DONE
    await session.flush()

    listing2 = await upsert_private_listing(
        session, _raw_listing(title="2005 Lexus GX 470 (price drop, new photos)"),
        parser_version="v1",
    )
    await session.flush()
    assert listing2.id == listing.id
    assert listing2.enrichment_status == EnrichmentStatus.PENDING
    assert listing2.valuation_status == ValuationStatus.PENDING


async def test_upsert_no_reset_on_idempotent_reingest(session: AsyncSession) -> None:
    listing = await upsert_private_listing(session, _raw_listing(), parser_version="v1")
    listing.enrichment_status = EnrichmentStatus.DONE
    await session.flush()
    listing2 = await upsert_private_listing(session, _raw_listing(), parser_version="v1")
    await session.flush()
    # Identical content (price unchanged) → no spurious cascade.
    assert listing2.enrichment_status == EnrichmentStatus.DONE
