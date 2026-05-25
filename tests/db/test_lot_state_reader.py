"""Tests for lot_action_history."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import UserAction
from carbuyer.db.lot_state import apply_user_action, lot_action_history
from carbuyer.db.models import Auction, AuctionLot


def _seed_lot(session: AsyncSession) -> AuctionLot:
    a = Auction(
        source="hibid", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    lot = AuctionLot(
        auction=a, source_lot_id="L1", url="https://x/L1", title="t",
    )
    session.add(lot)
    return lot


@pytest.mark.asyncio
async def test_lot_action_history_newest_first(
    session: AsyncSession,
) -> None:
    lot = _seed_lot(session)
    await session.flush()
    base = datetime.now(UTC)
    apply_user_action(
        session, lot, UserAction.INTERESTED,
        source="dashboard", now=base,
    )
    apply_user_action(
        session, lot, UserAction.BID_PLACED,
        max_bid_cad=Decimal("3000"),
        source="dashboard", now=base + timedelta(minutes=5),
    )
    apply_user_action(
        session, lot, UserAction.PURCHASED,
        source="dashboard", now=base + timedelta(minutes=10),
    )
    await session.commit()

    rows = await lot_action_history(session, lot.id)
    assert [r.user_action for r in rows] == [
        UserAction.PURCHASED,
        UserAction.BID_PLACED,
        UserAction.INTERESTED,
    ]
    bid_row = rows[1]
    assert bid_row.max_bid_cad == Decimal("3000")
    assert bid_row.source == "dashboard"


@pytest.mark.asyncio
async def test_lot_action_history_empty(
    session: AsyncSession,
) -> None:
    lot = _seed_lot(session)
    await session.commit()
    rows = await lot_action_history(session, lot.id)
    assert rows == []
