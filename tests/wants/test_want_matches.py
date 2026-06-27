from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Auction, AuctionLot, Search, WantMatch


async def _seed_search_and_lot(session: AsyncSession) -> tuple[Search, AuctionLot]:
    auction = Auction(
        source="test",
        source_auction_id="a1",
        url="http://x/auction",
        canonical_url="http://x/auction",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session.add(auction)
    await session.flush()
    lot = AuctionLot(auction_id=auction.id, source_lot_id="l1", url="http://x/lot")
    search = Search(name="manual xterra", config={})
    session.add_all([lot, search])
    await session.flush()
    return search, lot


def test_want_match_table_name() -> None:
    assert WantMatch.__tablename__ == "want_matches"


async def test_want_match_round_trip(session: AsyncSession) -> None:
    search, lot = await _seed_search_and_lot(session)
    wm = WantMatch(search_id=search.id, lot_id=lot.id)
    session.add(wm)
    await session.commit()
    wm_id, search_id, lot_id = wm.id, search.id, lot.id  # capture before expiry

    session.expire_all()
    fetched = (
        await session.execute(select(WantMatch).where(WantMatch.id == wm_id))
    ).scalar_one()
    assert fetched.search_id == search_id
    assert fetched.lot_id == lot_id
    assert fetched.dismissed is False
    assert fetched.notified_at is None
    assert fetched.want_relative_score is None
    assert fetched.created_at is not None  # TimestampMixin doubles as match time


async def test_want_match_unique_per_search_and_lot(session: AsyncSession) -> None:
    search, lot = await _seed_search_and_lot(session)
    session.add(WantMatch(search_id=search.id, lot_id=lot.id))
    await session.flush()
    session.add(WantMatch(search_id=search.id, lot_id=lot.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_search_cascades_to_its_matches(session: AsyncSession) -> None:
    search, lot = await _seed_search_and_lot(session)
    search_id = search.id
    session.add(WantMatch(search_id=search.id, lot_id=lot.id))
    await session.commit()

    await session.delete(search)
    await session.commit()
    session.expire_all()
    remaining = (
        await session.execute(select(WantMatch).where(WantMatch.search_id == search_id))
    ).scalars().all()
    assert remaining == []


async def test_deleting_lot_cascades_to_its_matches(session: AsyncSession) -> None:
    search, lot = await _seed_search_and_lot(session)
    lot_id = lot.id
    session.add(WantMatch(search_id=search.id, lot_id=lot.id))
    await session.commit()

    await session.delete(lot)
    await session.commit()
    session.expire_all()
    remaining = (
        await session.execute(select(WantMatch).where(WantMatch.lot_id == lot_id))
    ).scalars().all()
    assert remaining == []
