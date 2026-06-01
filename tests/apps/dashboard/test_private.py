from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.enums import UserAction
from carbuyer.db.models import (
    PrivateListing,
    SavedSearch,
    SavedSearchMatch,
)


@pytest.fixture
def _patch_deps(  # pyright: ignore[reportUnusedFunction]
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _listing(**kw: object) -> PrivateListing:
    """A minimal valid PrivateListing; override fields via kwargs."""
    defaults: dict[str, object] = {
        "source": "kijiji",
        "source_listing_id": "L1",
        "url": "https://www.kijiji.ca/v-cars-trucks/x/1",
        "canonical_url": "https://www.kijiji.ca/v-cars-trucks/x/1",
    }
    defaults.update(kw)
    return PrivateListing(**defaults)


@pytest.mark.asyncio
async def test_search_detail_renders_private_match(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    shown = _listing(source_listing_id="S1", title="Shown Private Mustang")
    passed = _listing(source_listing_id="S2", title="Passed Private Mustang",
                      user_action=UserAction.PASSED)
    s = SavedSearch(name="stangs", make="Ford")
    session.add_all([shown, passed, s])
    await session.flush()
    session.add_all([
        SavedSearchMatch(saved_search_id=s.id, source_kind="private_listing", source_id=shown.id),
        SavedSearchMatch(saved_search_id=s.id, source_kind="private_listing", source_id=passed.id),
    ])
    await session.commit()

    async with _client() as client:
        r = await client.get(f"/searches/{s.id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Shown Private Mustang" in r.text
    assert "Passed Private Mustang" not in r.text          # passed excluded
    assert "https://www.kijiji.ca/v-cars-trucks/x/1" in r.text  # links to the external listing
    assert f"/searches/{s.id}/dismiss/" in r.text  # matches list emits dismiss buttons


@pytest.mark.asyncio
async def test_search_detail_blocks_javascript_url(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    evil = _listing(source_listing_id="X1", title="Evil Listing",
                    url="javascript:alert(document.cookie)")
    s = SavedSearch(name="x", make="Ford")
    session.add_all([evil, s])
    await session.flush()
    session.add(SavedSearchMatch(
        saved_search_id=s.id, source_kind="private_listing", source_id=evil.id,
    ))
    await session.commit()

    async with _client() as client:
        r = await client.get(f"/searches/{s.id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Evil Listing" in r.text          # title still shown
    assert 'href="javascript:' not in r.text  # but NOT as a clickable javascript: link


@pytest.mark.asyncio
async def test_search_badges_count_private_matches(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    listing = _listing(title="Private Mustang", make="Ford", model="Mustang")
    s = SavedSearch(name="stangs", make="Ford")
    session.add_all([listing, s])
    await session.flush()
    session.add(SavedSearchMatch(
        saved_search_id=s.id, source_kind="private_listing", source_id=listing.id,
    ))
    await session.commit()

    async with _client() as client:
        r = await client.get("/searches")
    assert r.status_code == 200  # noqa: PLR2004
    assert "1 match" in r.text  # the private match is counted
    assert "0 matches" not in r.text
