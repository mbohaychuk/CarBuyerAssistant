from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import SavedSearch, SavedSearchMatch


@pytest.mark.asyncio
async def test_saved_search_defaults(session: AsyncSession) -> None:
    s = SavedSearch(name="60s Mustangs", make="Ford", model="Mustang")
    session.add(s)
    await session.flush()
    await session.refresh(s)
    assert s.id is not None
    assert s.is_active is True  # server_default true
    assert s.created_at is not None
    assert s.last_viewed_at is None  # never visited yet
    assert s.year_min is None and s.title_status is None  # NULL = wildcard


@pytest.mark.asyncio
async def test_saved_search_array_columns_roundtrip(session: AsyncSession) -> None:
    s = SavedSearch(
        name="AB/SK clean",
        province=["AB", "SK"],
        title_status=["NORMAL", "REBUILT"],
        condition_categorical=["good", "decent"],
    )
    session.add(s)
    await session.flush()
    await session.refresh(s)
    assert s.province == ["AB", "SK"]
    assert s.title_status == ["NORMAL", "REBUILT"]


@pytest.mark.asyncio
async def test_match_unique_and_cascade(session: AsyncSession) -> None:
    s = SavedSearch(name="x")
    session.add(s)
    await session.flush()

    m = SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=42)
    session.add(m)
    await session.flush()
    await session.refresh(m)
    assert m.matched_at is not None
    assert m.dismissed_at is None

    dup = SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=42)
    session.add(dup)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_match_cascade_on_search_delete(session: AsyncSession) -> None:
    s = SavedSearch(name="x")
    session.add(s)
    await session.flush()
    session.add(SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=1))
    await session.flush()
    await session.delete(s)
    await session.flush()
    remaining = (await session.execute(select(SavedSearchMatch))).scalars().all()
    assert remaining == []
