from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.valuator.valuator import value_one
from carbuyer.db.enums import NotificationStatus
from carbuyer.db.models import Auction, AuctionLot, WantMatch
from carbuyer.wants import repo, service
from carbuyer.wants.criteria import WantCriteria


async def _auction(session: AsyncSession) -> Auction:
    auction = Auction(
        source="test",
        source_auction_id="a1",
        url="http://x/a",
        canonical_url="http://x/a",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        pickup_province="AB",
    )
    session.add(auction)
    await session.flush()
    return auction


async def _lot(session: AsyncSession, auction: Auction, **over: Any) -> AuctionLot:
    base: dict[str, Any] = {
        "auction_id": auction.id,
        "source_lot_id": "l1",
        "url": "http://x/l1",
        "make": "Nissan",
        "model": "Xterra",
        "year": 2010,
        "current_high_bid_cad": Decimal("8000"),
        "expected_value_cad": Decimal("10000"),
        "value_mid_cad": Decimal("10000"),
        "comp_count": 9,
        "showstopper_flags": [],
    }
    base.update(over)
    lot = AuctionLot(**base)
    session.add(lot)
    await session.flush()
    return lot


async def _count_matches(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(WantMatch))).scalar_one()


async def test_evaluate_creates_match_with_score(session: AsyncSession) -> None:
    auction = await _auction(session)
    lot = await _lot(session, auction)
    want = await repo.create_want(
        session, name="x", criteria=WantCriteria(makes=["Nissan"], models=["Xterra"])
    )
    await session.flush()

    created = await service.evaluate_lot_against_wants(
        session, lot, pickup_province="AB", offer_price_cad=lot.current_high_bid_cad
    )
    assert [m.search_id for m in created] == [want.id]
    row = (
        await session.execute(select(WantMatch).where(WantMatch.search_id == want.id))
    ).scalar_one()
    assert row.want_relative_score == 0.2  # noqa: PLR2004 -- (10000-8000)/10000
    assert row.notified_at is None


async def test_evaluate_ignores_disabled_wants(session: AsyncSession) -> None:
    auction = await _auction(session)
    lot = await _lot(session, auction)
    want = await repo.create_want(session, name="x", criteria=WantCriteria(makes=["Nissan"]))
    await repo.update_want(session, want.id, enabled=False)
    await session.flush()

    created = await service.evaluate_lot_against_wants(
        session, lot, pickup_province="AB", offer_price_cad=Decimal("8000")
    )
    assert created == []
    assert await _count_matches(session) == 0


async def test_evaluate_no_match_when_make_differs(session: AsyncSession) -> None:
    auction = await _auction(session)
    lot = await _lot(session, auction)
    await repo.create_want(session, name="x", criteria=WantCriteria(makes=["Toyota"]))
    await session.flush()

    created = await service.evaluate_lot_against_wants(
        session, lot, pickup_province="AB", offer_price_cad=Decimal("8000")
    )
    assert created == []


async def test_evaluate_is_idempotent(session: AsyncSession) -> None:
    auction = await _auction(session)
    lot = await _lot(session, auction)
    await repo.create_want(session, name="x", criteria=WantCriteria(makes=["Nissan"]))
    await session.flush()

    first = await service.evaluate_lot_against_wants(
        session, lot, pickup_province="AB", offer_price_cad=Decimal("8000")
    )
    second = await service.evaluate_lot_against_wants(
        session, lot, pickup_province="AB", offer_price_cad=Decimal("8000")
    )
    assert len(first) == 1
    assert second == []
    assert await _count_matches(session) == 1


async def test_upsert_want_match_insert_then_update(session: AsyncSession) -> None:
    auction = await _auction(session)
    lot = await _lot(session, auction)
    want = await repo.create_want(session, name="x", criteria=WantCriteria())
    await session.flush()

    wm, created = await repo.upsert_want_match(
        session, search_id=want.id, lot_id=lot.id, want_relative_score=0.2
    )
    assert created is True
    wm2, created2 = await repo.upsert_want_match(
        session, search_id=want.id, lot_id=lot.id, want_relative_score=0.3
    )
    assert created2 is False
    assert wm2.id == wm.id
    assert wm2.want_relative_score == 0.3  # noqa: PLR2004 -- updated value


async def test_value_one_forces_pending_on_want_match(session: AsyncSession) -> None:
    # No comps in the DB → valuation is INSUFFICIENT (would SKIP notification),
    # but an explicit want match must still enqueue a notification.
    auction = await _auction(session)
    lot = await _lot(session, auction)
    want = await repo.create_want(
        session, name="x", criteria=WantCriteria(makes=["Nissan"], models=["Xterra"])
    )
    await session.flush()

    await value_one(session, lot)

    assert lot.notification_status == NotificationStatus.PENDING
    row = (await session.execute(select(WantMatch))).scalar_one()
    assert row.search_id == want.id
    assert row.lot_id == lot.id


async def test_value_one_keeps_skipped_without_want_match(session: AsyncSession) -> None:
    auction = await _auction(session)
    lot = await _lot(session, auction)
    await repo.create_want(session, name="x", criteria=WantCriteria(makes=["Toyota"]))
    await session.flush()

    await value_one(session, lot)

    assert lot.notification_status == NotificationStatus.SKIPPED
    assert await _count_matches(session) == 0
