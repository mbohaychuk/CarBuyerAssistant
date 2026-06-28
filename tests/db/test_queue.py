from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import EnrichmentStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, VehicleOffer
from carbuyer.db.queue import (
    claim_pending_ids,
    claim_pending_lots,
    recover_orphans,
    select_pending_ids,
)


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
async def test_claim_serves_both_auction_and_childless_private_offers(
    session: AsyncSession,
) -> None:
    """The whole point of the core-table FOR UPDATE claim: ONE queue serves both
    an AuctionLot (parent + auction_lot child) and a PrivateListing (parent, NO
    auction_lot child). The with_polymorphic entity select is an outer join, so
    a naive FOR UPDATE on the nullable child side would crash."""
    a = _seed_auction(session)
    await session.flush()
    session.add(AuctionLot(auction_id=a.id, source_lot_id="L1", url="u1"))
    session.add(PrivateListing(
        source="kijiji", source_listing_id="K1", url="http://k/1",
        asking_price_cad=Decimal("8000"), listing_status="active",
    ))
    await session.flush()

    lots = await claim_pending_lots(session, status_field="valuation_status", limit=10)

    kinds = sorted(type(lot).__name__ for lot in lots)
    assert kinds == ["AuctionLot", "PrivateListing"]  # both kinds claimed
    for lot in lots:
        assert lot.valuation_status == ValuationStatus.IN_PROGRESS
    # The private offer loaded as the right polymorphic subclass with child cols.
    listing = next(lot for lot in lots if isinstance(lot, PrivateListing))
    assert listing.asking_price_cad == Decimal("8000")
    # And it is genuinely childless (no auction_lot row).
    assert await session.get(AuctionLot, listing.id) is None
    assert await session.get(VehicleOffer, listing.id) is not None


@pytest.mark.asyncio
async def test_claim_pending_ids_returns_empty_when_no_pending(
    session: AsyncSession,
) -> None:
    ids = await claim_pending_ids(session, status_field="enrichment_status", limit=10)
    assert ids == []


@pytest.mark.asyncio
async def test_recover_orphans_flips_in_progress_back_to_pending(
    session: AsyncSession,
) -> None:
    """Phase 13: a worker crash leaves rows in IN_PROGRESS. The Phase 2.5
    watchdog is referenced but unbuilt; recover_orphans is called from each
    worker's catchup-sweep at startup. Single-instance worker invariant
    means unconditional recovery is safe."""
    a = _seed_auction(session)
    await session.flush()
    expected = 3
    for i in range(expected):
        session.add(AuctionLot(
            auction_id=a.id, source_lot_id=f"L{i}", url=f"u{i}",
            enrichment_status=EnrichmentStatus.IN_PROGRESS,
        ))
    # One control row in PENDING (must not be touched).
    session.add(AuctionLot(
        auction_id=a.id, source_lot_id="L_pending", url="u_p",
    ))
    # One control row in DONE (must not be touched).
    session.add(AuctionLot(
        auction_id=a.id, source_lot_id="L_done", url="u_d",
        enrichment_status=EnrichmentStatus.DONE,
    ))
    await session.flush()

    n = await recover_orphans(session, status_field="enrichment_status")
    assert n == expected
    await session.flush()

    statuses = [
        lot.enrichment_status
        for lot in (await session.execute(
            __import__("sqlalchemy").select(AuctionLot),
        )).scalars().all()
    ]
    # All 3 orphans → PENDING, the existing PENDING stays, the DONE stays.
    pending_count = sum(1 for s in statuses if s == EnrichmentStatus.PENDING)
    expected_pending = expected + 1
    assert pending_count == expected_pending
    assert EnrichmentStatus.IN_PROGRESS not in statuses
    assert EnrichmentStatus.DONE in statuses


@pytest.mark.asyncio
async def test_recover_orphans_returns_zero_when_no_in_progress(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    session.add(AuctionLot(auction_id=a.id, source_lot_id="L0", url="u0"))
    await session.flush()
    n = await recover_orphans(session, status_field="enrichment_status")
    assert n == 0


@pytest.mark.asyncio
async def test_recover_orphans_scoped_to_named_status_field(
    session: AsyncSession,
) -> None:
    """A lot stuck IN_PROGRESS on enrichment_status must NOT have its
    valuation_status touched. Scoping check."""
    a = _seed_auction(session)
    await session.flush()
    session.add(AuctionLot(
        auction_id=a.id, source_lot_id="L0", url="u0",
        enrichment_status=EnrichmentStatus.IN_PROGRESS,
        valuation_status=ValuationStatus.IN_PROGRESS,
    ))
    await session.flush()

    await recover_orphans(session, status_field="enrichment_status")
    await session.flush()
    lots = list((await session.execute(
        __import__("sqlalchemy").select(AuctionLot),
    )).scalars().all())
    assert lots[0].enrichment_status == EnrichmentStatus.PENDING
    assert lots[0].valuation_status == ValuationStatus.IN_PROGRESS  # untouched


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
