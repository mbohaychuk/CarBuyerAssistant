from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch


@pytest.fixture
def _patch_deps(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_preview_renders_matches_and_rare(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = Auction(source="t", source_auction_id="A", url="u", canonical_url="u",
                auction_subtype="estate", first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC), title="Graham Auctions",
                pickup_province="MB", scheduled_start_at=datetime.now(UTC) + timedelta(hours=10))
    session.add(a)
    await session.flush()
    matched = AuctionLot(
        auction=a, source_lot_id="L1", url="u1", title="Matched Mustang",
        make="Ford", model="Mustang", year=1968, lot_status="open",
    )
    rarelot = AuctionLot(
        auction=a, source_lot_id="L2", url="u2", title="Rare Viper",
        make="Dodge", model="Viper", year=2005, lot_status="open", rarity_score=4.0,
    )
    session.add_all([matched, rarelot])
    await session.flush()
    s = SavedSearch(name="60s Mustang", make="Ford")
    session.add(s)
    await session.flush()
    session.add(SavedSearchMatch(
        saved_search_id=s.id, source_kind="auction_lot", source_id=matched.id,
    ))
    await session.commit()

    async with _client() as client:
        r = await client.get(f"/auctions/{a.id}/digest")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Matched Mustang" in r.text or "Ford Mustang" in r.text
    assert "Rare Viper" in r.text or "Dodge Viper" in r.text


@pytest.mark.asyncio
async def test_preview_empty_state(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = Auction(source="t", source_auction_id="A", url="u", canonical_url="u",
                auction_subtype="estate", first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC), title="Empty Auction", pickup_province="MB")
    session.add(a)
    await session.commit()
    async with _client() as client:
        r = await client.get(f"/auctions/{a.id}/digest")
    assert r.status_code == 200  # noqa: PLR2004
    assert "nothing" in r.text.lower() or "no " in r.text.lower()


@pytest.mark.asyncio
async def test_preview_404_unknown_auction(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.get("/auctions/999999/digest")
    assert r.status_code == 404  # noqa: PLR2004
