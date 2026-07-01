"""S2 — private-listing write path: RawListing DTO + upsert_private_listing.

Proven with hand-built RawListings (as a ListingSource would yield); no real
scraping. Asserts the two-table write lands a vehicle_offer(offer_kind='private')
parent + a private_listing child under one id, keyed on (source,
source_listing_id), idempotent on re-ingest, with the status cascade firing on
content change.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import EnrichmentStatus, ListingStatus, ValuationStatus
from carbuyer.db.models import PrivateListing, Search, VehicleOffer, WantMatch
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


async def test_upsert_repends_valuation_on_price_change(session: AsyncSession) -> None:
    listing = await upsert_private_listing(
        session, _raw_listing(asking="15000"), parser_version="v1",
    )
    listing.enrichment_status = EnrichmentStatus.DONE
    listing.valuation_status = ValuationStatus.DONE
    await session.flush()

    # Price-only re-ingest (content unchanged) must re-value so the deal score +
    # want matching re-run, but must NOT re-enrich (make/model unchanged).
    listing2 = await upsert_private_listing(
        session, _raw_listing(asking="14000"), parser_version="v1",
    )
    await session.flush()
    assert listing2.asking_price_cad == Decimal("14000")
    assert listing2.valuation_status == ValuationStatus.PENDING
    assert listing2.enrichment_status == EnrichmentStatus.DONE


async def _notified_match(session: AsyncSession, listing: PrivateListing) -> int:
    """Seed a want + an already-notified want_match for the listing; return wm id."""
    want = Search(name="gx", config={})
    session.add(want)
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=listing.id, notified_at=datetime.now(UTC))
    session.add(wm)
    await session.flush()
    return wm.id


async def test_price_drop_records_previous_and_re_alerts(session: AsyncSession) -> None:
    listing = await upsert_private_listing(
        session, _raw_listing(asking="15000"), parser_version="v1",
    )
    wm_id = await _notified_match(session, listing)

    listing2 = await upsert_private_listing(
        session, _raw_listing(asking="13500"), parser_version="v1",
    )
    await session.flush()
    assert listing2.previous_asking_price_cad == Decimal("15000")

    session.expire_all()
    wm = await session.get(WantMatch, wm_id)
    assert wm is not None
    assert wm.notified_at is None  # fire-once cleared → notifier re-delivers


async def test_price_increase_records_previous_but_does_not_re_alert(
    session: AsyncSession,
) -> None:
    listing = await upsert_private_listing(
        session, _raw_listing(asking="15000"), parser_version="v1",
    )
    wm_id = await _notified_match(session, listing)

    listing2 = await upsert_private_listing(
        session, _raw_listing(asking="16000"), parser_version="v1",
    )
    await session.flush()
    assert listing2.previous_asking_price_cad == Decimal("15000")

    session.expire_all()
    wm = await session.get(WantMatch, wm_id)
    assert wm is not None
    assert wm.notified_at is not None  # an increase must NOT re-alert


async def test_upsert_no_reset_on_idempotent_reingest(session: AsyncSession) -> None:
    listing = await upsert_private_listing(session, _raw_listing(), parser_version="v1")
    listing.enrichment_status = EnrichmentStatus.DONE
    await session.flush()
    listing2 = await upsert_private_listing(session, _raw_listing(), parser_version="v1")
    await session.flush()
    # Identical content (price unchanged) → no spurious cascade.
    assert listing2.enrichment_status == EnrichmentStatus.DONE


# ─── buyer-leverage: original asking + drop count ───


async def _upsert(session: AsyncSession, asking: str) -> PrivateListing:
    return await upsert_private_listing(
        session, _raw_listing(asking=asking), parser_version="v1",
    )


async def test_upsert_insert_sets_original_and_zero_drop_count(session: AsyncSession) -> None:
    listing = await _upsert(session, "18000")
    await session.flush()
    assert listing.original_asking_price_cad == Decimal("18000")
    assert listing.price_drop_count == 0


async def test_price_drop_increments_count_and_keeps_original(session: AsyncSession) -> None:
    await _upsert(session, "18000")
    await session.flush()
    listing = await _upsert(session, "15000")
    await session.flush()
    assert listing.price_drop_count == 1
    assert listing.previous_asking_price_cad == Decimal("18000")
    assert listing.original_asking_price_cad == Decimal("18000")  # baseline unchanged
    listing = await _upsert(session, "14000")
    await session.flush()
    assert listing.price_drop_count == 2  # noqa: PLR2004


async def test_price_increase_does_not_increment_drop_count(session: AsyncSession) -> None:
    await _upsert(session, "15000")
    await session.flush()
    listing = await _upsert(session, "16000")
    await session.flush()
    assert listing.price_drop_count == 0
    assert listing.original_asking_price_cad == Decimal("15000")


async def test_drop_backfills_null_original(session: AsyncSession) -> None:
    await _upsert(session, "18000")
    await session.flush()
    listing = (await session.execute(select(PrivateListing))).scalars().first()
    listing.original_asking_price_cad = None  # mimic a pre-feature row
    await session.flush()
    listing = await _upsert(session, "15000")
    await session.flush()
    assert listing.original_asking_price_cad == Decimal("18000")  # backfilled from pre-drop price
    assert listing.price_drop_count == 1
