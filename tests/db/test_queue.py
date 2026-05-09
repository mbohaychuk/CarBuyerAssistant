from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import EnrichmentStatus
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.queue import claim_pending_ids, select_pending_ids


def _seed_auction(session: AsyncSession) -> Auction:
    a = Auction(
        source="test", source_auction_id="A1", url="x",
        canonical_url="x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    return a


@pytest.mark.asyncio
async def test_claim_pending_ids_marks_in_progress(session: AsyncSession) -> None:
    a = _seed_auction(session)
    await session.flush()
    for i in range(3):
        session.add(AuctionLot(
            auction_id=a.id, source_lot_id=f"L{i}", url=f"u{i}",
        ))
    await session.flush()

    expected = 2
    ids = await claim_pending_ids(session, status_field="enrichment_status", limit=expected)
    assert len(ids) == expected

    for lot_id in ids:
        lot = await session.get(AuctionLot, lot_id)
        assert lot is not None
        assert lot.enrichment_status == EnrichmentStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_claim_pending_ids_returns_empty_when_no_pending(
    session: AsyncSession,
) -> None:
    ids = await claim_pending_ids(session, status_field="enrichment_status", limit=10)
    assert ids == []


@pytest.mark.asyncio
async def test_select_pending_ids_does_not_modify_rows(session: AsyncSession) -> None:
    a = _seed_auction(session)
    await session.flush()
    session.add(AuctionLot(
        auction_id=a.id, source_lot_id="L0", url="u0",
    ))
    await session.flush()

    ids = await select_pending_ids(session, status_field="enrichment_status")
    assert len(ids) == 1
    lot = await session.get(AuctionLot, ids[0])
    assert lot is not None
    assert lot.enrichment_status == EnrichmentStatus.PENDING
