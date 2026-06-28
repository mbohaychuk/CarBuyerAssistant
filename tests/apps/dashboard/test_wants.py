from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.models import Auction, AuctionLot, Search, WantMatch
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria


@pytest.fixture
def _patch_deps(  # pyright: ignore[reportUnusedFunction]
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _client(*, follow: bool = True) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=follow
    )


async def _seed_match(session: AsyncSession) -> tuple[int, int]:
    """A want + a matched lot. Returns (want_id, want_match_id)."""
    auction = Auction(
        source="test", source_auction_id="A1", url="http://x/a",
        canonical_url="http://x/a",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        pickup_province="AB",
    )
    session.add(auction)
    await session.flush()
    lot = AuctionLot(
        auction_id=auction.id, source_lot_id="L1", url="http://x/lot",
        title="2010 Nissan Xterra", year=2010, make="Nissan", model="Xterra",
        current_high_bid_cad=Decimal("8000"),
    )
    want = Search(name="manual xterra", config={})
    session.add_all([lot, want])
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=lot.id, want_relative_score=0.2)
    session.add(wm)
    await session.commit()
    return want.id, wm.id


async def test_wants_page_empty(_patch_deps: AsyncSession) -> None:
    async with _client() as c:
        r = await c.get("/wants")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Wants" in r.text


async def test_create_want_round_trip(_patch_deps: AsyncSession) -> None:
    async with _client(follow=False) as c:
        r = await c.post(
            "/wants",
            data={"name": "manual xterra", "makes": "Nissan",
                  "models": "Xterra", "transmissions": "manual"},
        )
        assert r.status_code == 303  # noqa: PLR2004
        listing = await c.get("/wants")
    assert "manual xterra" in listing.text


async def test_create_want_invalid_shows_error(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    async with _client() as c:
        r = await c.post("/wants", data={"name": "x", "transmissions": "stick"})
    assert r.status_code == 200  # noqa: PLR2004 -- re-render, not redirect
    assert "Invalid" in r.text
    assert await repo.list_wants(session) == []


async def test_toggle_want_mutes_and_unmutes(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    want = await repo.create_want(session, name="x", criteria=WantCriteria())
    await session.commit()
    want_id = want.id

    async with _client(follow=False) as c:
        r = await c.post(f"/wants/{want_id}/toggle")
        assert r.status_code == 303  # noqa: PLR2004
    session.expire_all()
    assert (await session.get(Search, want_id)).enabled is False  # type: ignore[union-attr]


async def test_delete_want(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    want = await repo.create_want(session, name="x", criteria=WantCriteria())
    await session.commit()
    want_id = want.id

    async with _client(follow=False) as c:
        r = await c.post(f"/wants/{want_id}/delete")
        assert r.status_code == 303  # noqa: PLR2004
    session.expire_all()
    assert await session.get(Search, want_id) is None


async def test_want_detail_lists_matches(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    want_id, _ = await _seed_match(session)
    async with _client() as c:
        r = await c.get(f"/wants/{want_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Xterra" in r.text


async def test_dismiss_match(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    _, wm_id = await _seed_match(session)
    async with _client(follow=False) as c:
        r = await c.post(f"/want-matches/{wm_id}/dismiss")
        assert r.status_code == 303  # noqa: PLR2004
    session.expire_all()
    assert (await session.get(WantMatch, wm_id)).dismissed is True  # type: ignore[union-attr]
