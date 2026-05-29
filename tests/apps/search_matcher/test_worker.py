from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.search_matcher import worker as worker_mod
from carbuyer.apps.search_matcher.worker import (  # pyright: ignore[reportPrivateUsage]
    process_lot,
    process_search,
    startup_backfill,
)
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch


def _seed_auction(session: AsyncSession, *, province: str = "AB") -> Auction:
    a = Auction(
        source="test", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        pickup_province=province,
    )
    session.add(a)
    return a


def _seed_lot(
    session: AsyncSession, auction: Auction, *,
    source_lot_id: str = "L1", make: str | None = "Ford",
    model: str | None = "Mustang", year: int | None = 1968,
    title_status: str = "NORMAL", lot_status: str = "open",
    all_in: Decimal | None = Decimal("25000"),
) -> AuctionLot:
    lot = AuctionLot(
        auction=auction, source_lot_id=source_lot_id,
        url=f"https://x/{source_lot_id}", title="car",
        make=make, model=model, year=year, title_status=title_status,
        lot_status=lot_status, all_in_at_current_bid_cad=all_in,
    )
    session.add(lot)
    return lot


@pytest.fixture
def _patched_get_session(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(worker_mod, "get_session", fake_get_session)
    return session


async def _matches(session: AsyncSession, search_id: int) -> list[int]:
    rows = (await session.execute(
        select(SavedSearchMatch.source_id)
        .where(SavedSearchMatch.saved_search_id == search_id)
    )).scalars().all()
    return sorted(rows)


@pytest.mark.asyncio
async def test_process_lot_records_match_for_active_search(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    lot = _seed_lot(session, a)
    s = SavedSearch(name="stangs", make="Ford", model="Mustang")
    session.add(s)
    await session.flush()

    n = await process_lot(lot.id)
    assert n == 1
    assert await _matches(session, s.id) == [lot.id]


@pytest.mark.asyncio
async def test_process_lot_is_idempotent(_patched_get_session: AsyncSession) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    lot = _seed_lot(session, a)
    s = SavedSearch(name="stangs", make="Ford")
    session.add(s)
    await session.flush()

    assert await process_lot(lot.id) == 1
    assert await process_lot(lot.id) == 1  # ON CONFLICT DO NOTHING
    assert await _matches(session, s.id) == [lot.id]


@pytest.mark.asyncio
async def test_process_lot_skips_inactive_search(_patched_get_session: AsyncSession) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    lot = _seed_lot(session, a)
    s = SavedSearch(name="off", make="Ford", is_active=False)
    session.add(s)
    await session.flush()
    assert await process_lot(lot.id) == 0
    assert await _matches(session, s.id) == []


@pytest.mark.asyncio
async def test_process_lot_missing_returns_zero(_patched_get_session: AsyncSession) -> None:
    assert await process_lot(999_999) == 0


@pytest.mark.asyncio
async def test_process_search_backfills_active_lots_only(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    open_lot = _seed_lot(session, a, source_lot_id="L1", lot_status="open")
    closed_lot = _seed_lot(session, a, source_lot_id="L2", lot_status="closed")
    s = SavedSearch(name="stangs", make="Ford")
    session.add(s)
    await session.flush()

    n = await process_search(s.id)
    assert n == 1
    assert await _matches(session, s.id) == [open_lot.id]
    assert closed_lot.id not in await _matches(session, s.id)


@pytest.mark.asyncio
async def test_startup_backfill_matches_cross_product(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    ford = _seed_lot(session, a, source_lot_id="L1", make="Ford")
    toyota = _seed_lot(session, a, source_lot_id="L2", make="Toyota")
    s_ford = SavedSearch(name="ford", make="Ford")
    s_any = SavedSearch(name="any")
    session.add_all([s_ford, s_any])
    await session.flush()

    await startup_backfill()
    assert await _matches(session, s_ford.id) == [ford.id]
    assert await _matches(session, s_any.id) == sorted([ford.id, toyota.id])
