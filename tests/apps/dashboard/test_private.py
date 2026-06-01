from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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


@pytest.mark.asyncio
async def test_private_feed_lists_and_excludes_removed_and_passed(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    live = _listing(
        source_listing_id="F1", title="Live Jeep", make="Jeep", model="Cherokee",
        year=2016, ask_price_cad=Decimal("13999"), expected_value_cad=Decimal("16500"),
        price_deal_score=0.18, condition_categorical="good",
    )
    removed = _listing(source_listing_id="F2", title="Removed Jeep",
                       removed_at=datetime.now(UTC), price_deal_score=0.5)
    passed = _listing(source_listing_id="F3", title="Passed Jeep",
                      user_action=UserAction.PASSED, price_deal_score=0.9)
    session.add_all([live, removed, passed])
    await session.commit()

    async with _client() as client:
        r = await client.get("/private")
    assert r.status_code == 200  # noqa: PLR2004
    assert "2016 Jeep Cherokee" in r.text
    assert "Removed Jeep" not in r.text
    assert "Passed Jeep" not in r.text


@pytest.mark.asyncio
async def test_private_feed_orders_best_deal_first(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    weak = _listing(source_listing_id="O1", title="Weak deal", make="A", model="x",
                    price_deal_score=0.05)
    strong = _listing(source_listing_id="O2", title="Strong deal", make="B", model="y",
                      price_deal_score=0.40)
    session.add_all([weak, strong])
    await session.commit()

    async with _client() as client:
        r = await client.get("/private")
    assert r.status_code == 200  # noqa: PLR2004
    assert (
        r.text.index(f'id="private-{strong.id}"')
        < r.text.index(f'id="private-{weak.id}"')
    )


@pytest.mark.asyncio
async def test_private_in_topnav(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.get("/private")
    assert 'href="/private"' in r.text
    assert 'aria-current="page"' in r.text  # Private is the active nav item


@pytest.mark.asyncio
async def test_private_card_blocks_javascript_url(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    evil = _listing(source_listing_id="EV", title="Evil", make="Ford", model="F150",
                    url="javascript:alert(document.cookie)", price_deal_score=0.2)
    session.add(evil)
    await session.commit()
    async with _client() as client:
        r = await client.get("/private")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Ford F150" in r.text            # title still shown
    assert 'href="javascript:' not in r.text  # but not as a clickable link


@pytest.mark.asyncio
async def test_mark_interested_sets_action_and_returns_card(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    listing = _listing(source_listing_id="M1", title="Markable", make="Ford", model="F150")
    session.add(listing)
    await session.commit()

    async with _client() as client:
        r = await client.post(
            f"/private/{listing.id}/mark",
            data={"action": "interested", "currently_active": "false"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200  # noqa: PLR2004
    assert f'id="private-{listing.id}"' in r.text  # card fragment returned
    await session.refresh(listing)
    assert listing.user_action == UserAction.INTERESTED


@pytest.mark.asyncio
async def test_mark_passed_returns_empty_so_card_drops(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    listing = _listing(source_listing_id="M2", title="Passable", make="Ford", model="F150")
    session.add(listing)
    await session.commit()

    async with _client() as client:
        r = await client.post(
            f"/private/{listing.id}/mark",
            data={"action": "passed", "currently_active": "false"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200  # noqa: PLR2004
    assert r.text.strip() == ""  # empty body -> HTMX swaps the card away
    await session.refresh(listing)
    assert listing.user_action == UserAction.PASSED


@pytest.mark.asyncio
async def test_mark_toggle_off_clears_action(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    listing = _listing(source_listing_id="M3", title="Toggle", make="Ford", model="F150",
                       user_action=UserAction.INTERESTED)
    session.add(listing)
    await session.commit()

    async with _client() as client:
        r = await client.post(
            f"/private/{listing.id}/mark",
            data={"action": "interested", "currently_active": "true"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200  # noqa: PLR2004
    await session.refresh(listing)
    assert listing.user_action is None


@pytest.mark.asyncio
async def test_mark_unknown_listing_404(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.post(
            "/private/999999/mark",
            data={"action": "interested", "currently_active": "false"},
        )
    assert r.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_mark_invalid_action_422(_patch_deps: AsyncSession) -> None:
    # Auction-only actions (e.g. bid_placed) are not valid on a private listing.
    session = _patch_deps
    listing = _listing(source_listing_id="M4", title="X", make="Ford", model="F150")
    session.add(listing)
    await session.commit()
    async with _client() as client:
        r = await client.post(
            f"/private/{listing.id}/mark",
            data={"action": "bid_placed", "currently_active": "false"},
        )
    assert r.status_code == 422  # noqa: PLR2004
    await session.refresh(listing)
    assert listing.user_action is None  # rejected action left state untouched
