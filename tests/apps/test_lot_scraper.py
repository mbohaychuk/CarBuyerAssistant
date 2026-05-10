from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.lot_scraper.scraper import upsert_lot_with_status_cascade
from carbuyer.db.enums import (
    EnrichmentStatus,
    LotStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import Auction
from carbuyer.sources.base import LotRef, RawLot


def _seed_auction(session: AsyncSession) -> Auction:
    a = Auction(
        source="test", source_auction_id="A1", url="x",
        canonical_url="x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    return a


def _lot_raw(title: str | None = "1995 Ford F-150", **overrides: Any) -> RawLot:
    base: dict[str, Any] = {
        "ref": LotRef(
            source="test", source_auction_id="A1", source_lot_id="L1",
            url="https://x/lot/1",
        ),
        "lot_number": "1",
        "title": title,
        "description": "runs and drives",
        "photos": ["https://x/p1.jpg"],
        "year": 1995, "make": "Ford", "model": "F-150",
        "current_high_bid_cad": Decimal("2500"),
        "scheduled_end_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return RawLot(**base)


@pytest.mark.asyncio
async def test_upsert_lot_inserts_with_parser_version(session: AsyncSession) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v1",
    )
    await session.flush()
    assert lot.id is not None
    assert lot.title == "1995 Ford F-150"
    assert lot.parser_version == "v1"
    assert lot.enrichment_status == EnrichmentStatus.PENDING


@pytest.mark.asyncio
async def test_upsert_lot_resets_statuses_when_content_changes(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v1",
    )
    lot.enrichment_status = EnrichmentStatus.DONE
    lot.valuation_status = ValuationStatus.DONE
    lot.vision_status = VisionStatus.DONE
    lot.notification_status = NotificationStatus.DONE
    await session.flush()

    lot2 = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(title="1995 Ford F-150 (revised)"),
        parser_version="v1",
    )
    await session.flush()
    assert lot2.id == lot.id
    assert lot2.title == "1995 Ford F-150 (revised)"
    assert lot2.enrichment_status == EnrichmentStatus.PENDING
    assert lot2.valuation_status == ValuationStatus.PENDING
    assert lot2.vision_status == VisionStatus.PENDING
    assert lot2.notification_status == NotificationStatus.PENDING


@pytest.mark.asyncio
async def test_upsert_lot_resets_when_parser_version_changes(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v1",
    )
    lot.enrichment_status = EnrichmentStatus.DONE
    await session.flush()
    lot2 = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v2",
    )
    await session.flush()
    assert lot2.parser_version == "v2"
    assert lot2.enrichment_status == EnrichmentStatus.PENDING


@pytest.mark.asyncio
async def test_upsert_lot_does_not_overwrite_with_none(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(title="t1"), parser_version="v1",
    )
    await session.flush()
    lot = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(title=None), parser_version="v1",
    )
    await session.flush()
    assert lot.title == "t1"


@pytest.mark.asyncio
async def test_upsert_lot_does_not_clobber_bid_poller_lot_status(
    session: AsyncSession,
) -> None:
    """Bid-poller writes lot_status='closing_soon'/'extended'/'closed';
    lot-scraper must NOT overwrite that on subsequent re-scrapes."""
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v1",
    )
    assert lot.lot_status == LotStatus.OPEN
    # Simulate bid-poller advancing the lot status.
    lot.lot_status = LotStatus.CLOSING_SOON
    await session.flush()
    # Re-scrape: raw still says lot_status='open' (default from HiBid parser).
    lot2 = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v1",
    )
    await session.flush()
    assert lot2.lot_status == LotStatus.CLOSING_SOON


@pytest.mark.asyncio
async def test_upsert_lot_preserves_vision_skipped_on_content_change(
    session: AsyncSession,
) -> None:
    """vision_status='skipped' is set by the vision-batcher when a lot is
    outside the top-10% deal-score gate. Don't reset it on content change —
    re-running burns OpenAI vision-API budget on already-judged lots."""
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v1",
    )
    lot.vision_status = VisionStatus.SKIPPED
    lot.enrichment_status = EnrichmentStatus.DONE
    await session.flush()
    # Content-changing re-scrape.
    lot2 = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(title="rev"), parser_version="v1",
    )
    await session.flush()
    assert lot2.vision_status == VisionStatus.SKIPPED  # preserved
    # But other statuses still reset on content change:
    assert lot2.enrichment_status == EnrichmentStatus.PENDING


@pytest.mark.asyncio
async def test_rescrape_preserves_llm_normalized_fields(
    session: AsyncSession,
) -> None:
    """Phase 3 design overlay #5: enricher normalizes year/make/model/trim/
    vin/mileage_km from raw heuristic values. A subsequent rescrape must NOT
    clobber the normalized value with the same raw heuristic value, otherwise
    the cascade fires forever (enrich → rescrape-clobber → re-enrich → ...).
    Lot-scraper writes these columns only on INSERT, never on UPDATE.
    """
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(model="F150"), parser_version="v1",
    )
    await session.flush()
    assert lot.model == "F150"
    # Simulate enricher normalization to canonical "F-150".
    lot.model = "F-150"
    lot.trim = "XLT"
    lot.vin = "1FTRX18W2WKA12345"
    lot.enrichment_status = EnrichmentStatus.DONE
    await session.flush()

    # Rescrape: raw heuristic still says "F150" (no enricher in real flow).
    lot2 = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(model="F150"), parser_version="v1",
    )
    await session.flush()
    assert lot2.id == lot.id
    # Normalized values preserved — not clobbered to raw "F150".
    assert lot2.model == "F-150"
    assert lot2.trim == "XLT"
    assert lot2.vin == "1FTRX18W2WKA12345"
    # Cascade did not fire because no genuine content change.
    assert lot2.enrichment_status == EnrichmentStatus.DONE


@pytest.mark.asyncio
async def test_upsert_lot_no_status_reset_on_idempotent_re_scrape(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v1",
    )
    lot.enrichment_status = EnrichmentStatus.DONE
    await session.flush()
    # Re-scrape with identical content + same parser version.
    lot2 = await upsert_lot_with_status_cascade(
        session, a.id, _lot_raw(), parser_version="v1",
    )
    await session.flush()
    # Status preserved — no spurious cascade.
    assert lot2.enrichment_status == EnrichmentStatus.DONE
