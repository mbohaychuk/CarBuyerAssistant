from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.apps.dashboard.routers import searches as searches_mod
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch


@pytest.fixture
def _patch_deps(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_create_persists_row_and_notifies(
    _patch_deps: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patch_deps
    sent: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str = "") -> None:
        sent.append((channel, payload))

    # The router imports notify into its own module namespace, so patch it there.
    monkeypatch.setattr(searches_mod, "notify", fake_notify)

    async with _client() as client:
        r = await client.post("/searches", data={
            "name": "60s Mustangs", "make": "Ford", "model": "Mustang",
            "year_min": "1965", "year_max": "1970",
            "title_status": ["NORMAL"], "province": ["AB", "SK"],
        })
    # httpx does not follow redirects by default; create returns a 303.
    assert r.status_code == 303  # noqa: PLR2004

    rows = (await session.execute(select(SavedSearch))).scalars().all()
    assert len(rows) == 1
    assert rows[0].make == "Ford"
    assert rows[0].province == ["AB", "SK"]
    assert any(ch == "saved_search_changed" for ch, _ in sent)


@pytest.mark.asyncio
async def test_list_renders_cards(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    session.add(SavedSearch(name="Trucks", make="Toyota"))
    await session.commit()
    async with _client() as client:
        r = await client.get("/searches")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Trucks" in r.text


@pytest.mark.asyncio
async def test_detail_shows_matches_excluding_passed(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = Auction(
        source="t", source_auction_id="A", url="u", canonical_url="u",
        auction_subtype="estate", first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC), pickup_province="AB",
    )
    session.add(a)
    await session.flush()
    shown = AuctionLot(auction=a, source_lot_id="L1", url="u1", title="Shown Mustang",
                       make="Ford", model="Mustang", lot_status="open")
    passed = AuctionLot(auction=a, source_lot_id="L2", url="u2", title="Passed Mustang",
                        make="Ford", model="Mustang", lot_status="open")
    session.add_all([shown, passed])
    await session.flush()
    passed.user_action = UserAction.PASSED
    s = SavedSearch(name="stangs", make="Ford")
    session.add(s)
    await session.flush()
    session.add_all([
        SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=shown.id),
        SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=passed.id),
    ])
    await session.commit()

    async with _client() as client:
        r = await client.get(f"/searches/{s.id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Shown Mustang" in r.text
    assert "Passed Mustang" not in r.text


@pytest.mark.asyncio
async def test_detail_stamps_last_viewed_at(_patch_deps: AsyncSession) -> None:
    """Opening the detail view marks the search visited so the list's "N new"
    badge can reset (matches newer than last_viewed_at)."""
    session = _patch_deps
    s = SavedSearch(name="x", make="Ford")
    session.add(s)
    await session.commit()
    assert s.last_viewed_at is None
    async with _client() as client:
        r = await client.get(f"/searches/{s.id}")
    assert r.status_code == 200  # noqa: PLR2004
    await session.refresh(s)
    assert s.last_viewed_at is not None


@pytest.mark.asyncio
async def test_dismiss_sets_dismissed_at(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    s = SavedSearch(name="x", make="Ford")
    session.add(s)
    await session.flush()
    m = SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=7)
    session.add(m)
    await session.commit()
    async with _client() as client:
        r = await client.post(f"/searches/{s.id}/dismiss/{m.id}")
    assert r.status_code == 200  # noqa: PLR2004
    await session.refresh(m)
    assert m.dismissed_at is not None


@pytest.mark.asyncio
async def test_delete_cascades(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    s = SavedSearch(name="x")
    session.add(s)
    await session.flush()
    session.add(SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=1))
    await session.commit()
    sid = s.id
    async with _client() as client:
        r = await client.post(f"/searches/{sid}/delete")
    assert r.status_code in (200, 204, 303)
    session.expire_all()  # flush identity map so the next get hits the DB
    assert (await session.get(SavedSearch, sid)) is None
    remaining = (await session.execute(
        select(SavedSearchMatch).where(SavedSearchMatch.saved_search_id == sid)
    )).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_update_search_deactivates_and_notifies(
    _patch_deps: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /searches/{id}/update: field changes persist, omitted is_active → False,
    and a saved_search_changed NOTIFY fires."""
    session = _patch_deps
    sent: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str = "") -> None:
        sent.append((channel, payload))

    monkeypatch.setattr(searches_mod, "notify", fake_notify)

    s = SavedSearch(name="Trucks", make="Toyota", is_active=True)
    session.add(s)
    await session.commit()

    async with _client() as client:
        # Omit is_active to simulate an unchecked checkbox.
        r = await client.post(f"/searches/{s.id}/update", data={
            "name": "Trucks", "make": "Honda",
        })
    assert r.status_code == 303  # noqa: PLR2004

    await session.refresh(s)
    assert s.make == "Honda"
    assert s.is_active is False
    assert any(ch == "saved_search_changed" for ch, _ in sent)


@pytest.mark.asyncio
async def test_watched_shows_subtab_strip(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.get("/watched")
    assert r.status_code == 200  # noqa: PLR2004
    assert 'href="/searches"' in r.text  # sub-tab links to Searches
    assert 'aria-label="Watchlist views"' in r.text


@pytest.mark.asyncio
async def test_searches_list_shows_subtab_strip(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.get("/searches")
    assert r.status_code == 200  # noqa: PLR2004
    assert 'href="/watched"' in r.text


@pytest.mark.asyncio
async def test_match_count_excludes_passed(_patch_deps: AsyncSession) -> None:
    """_match_count (surfaced via GET /searches) must not count passed lots."""
    session = _patch_deps
    a = Auction(
        source="t", source_auction_id="B", url="u2", canonical_url="u2",
        auction_subtype="estate", first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC), pickup_province="BC",
    )
    session.add(a)
    await session.flush()
    open_lot = AuctionLot(
        auction=a, source_lot_id="M1", url="u3", title="Open Truck",
        make="Toyota", model="Tacoma", lot_status="open",
    )
    passed_lot = AuctionLot(
        auction=a, source_lot_id="M2", url="u4", title="Passed Truck",
        make="Toyota", model="Tacoma", lot_status="open",
    )
    session.add_all([open_lot, passed_lot])
    await session.flush()
    passed_lot.user_action = UserAction.PASSED
    s = SavedSearch(name="tacomas", make="Toyota")
    session.add(s)
    await session.flush()
    session.add_all([
        SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=open_lot.id),
        SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=passed_lot.id),
    ])
    await session.commit()

    async with _client() as client:
        r = await client.get("/searches")
    assert r.status_code == 200  # noqa: PLR2004
    # Badge should show 1 match (open), not 2 (open + passed).
    assert "1 match" in r.text
    assert "2 match" not in r.text
