from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.apps.dashboard.routers.wants import _parse_model_specs
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, Search, WantMatch
from carbuyer.llm.schemas import ExpandedModel
from carbuyer.wants import repo
from carbuyer.wants.criteria import ModelSpec, WantCriteria


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


async def test_create_want_backfills_existing_matches(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    auction = Auction(
        source="test", source_auction_id="A1", url="http://x/a",
        canonical_url="http://x/a",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        pickup_province="AB",
    )
    session.add(auction)
    await session.flush()
    session.add(AuctionLot(
        auction_id=auction.id, source_lot_id="L1", url="http://x/lot",
        make="Nissan", model="Xterra", year=2010,
        current_high_bid_cad=Decimal("8000"),
        lot_status="open", valuation_status="done",
    ))
    await session.commit()

    async with _client(follow=False) as c:
        r = await c.post("/wants", data={"name": "x", "makes": "Nissan", "models": "Xterra"})
        assert r.status_code == 303  # noqa: PLR2004
        listing = await c.get("/wants")
    assert "1 match" in listing.text


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


async def test_expand_endpoint_renders_rows(
    _patch_deps: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_expand(text: str) -> list[ExpandedModel]:
        return [ExpandedModel(make="Lexus", model="GX 470", year_min=2003, year_max=2009,
                              trims=[], reason="J120 4Runner platform")]
    monkeypatch.setattr("carbuyer.apps.dashboard.routers.wants._expand", fake_expand)

    async with _client() as c:
        r = await c.post("/wants/expand", data={"archetype_text": "4runner platform"})
    assert r.status_code == 200  # noqa: PLR2004
    assert "GX 470" in r.text
    assert "2003" in r.text
    assert "J120 4Runner platform" in r.text


async def test_want_detail_shows_private_listing_match(_patch_deps: AsyncSession) -> None:
    """FIX 4: want_detail must show PrivateListing matches, not only AuctionLot rows."""
    session = _patch_deps
    listing = PrivateListing(
        source="kijiji", source_listing_id="PL1", url="http://k/1",
        title="2010 Nissan Xterra", make="Nissan", model="Xterra", year=2010,
        asking_price_cad=Decimal("8000"), listing_status="active",
    )
    want = Search(name="private xterra", config={})
    session.add_all([listing, want])
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=listing.id, want_relative_score=0.2)
    session.add(wm)
    await session.commit()

    async with _client() as c:
        r = await c.get(f"/wants/{want.id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Xterra" in r.text


async def test_create_want_with_no_makes_or_models_shows_error(_patch_deps: AsyncSession) -> None:
    """FIX 5: submitting the archetype form with all rows unchecked (empty model_specs)
    and no flat makes/models must not create a match-everything want."""
    session = _patch_deps
    async with _client() as c:
        r = await c.post("/wants", data={"name": "empty-want"})
    assert r.status_code == 200  # noqa: PLR2004 -- re-render with error, not redirect
    assert "make/model" in r.text.lower()
    assert await repo.list_wants(session) == []


def test_parse_model_specs_keeps_only_included_rows() -> None:
    rows = _parse_model_specs(
        ["A", "B", "C"], ["M1", "M2", "M3"], [], [], [], ["0", "2"],
    )
    assert [(s.make, s.model) for s in rows] == [("A", "M1"), ("C", "M3")]


async def test_expand_endpoint_handles_provider_error(
    _patch_deps: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(text: str) -> list[ModelSpec]:
        raise RuntimeError("openai down")
    monkeypatch.setattr("carbuyer.apps.dashboard.routers.wants._expand", boom)

    async with _client() as c:
        r = await c.post("/wants/expand", data={"archetype_text": "x"})
    assert r.status_code == 200  # noqa: PLR2004
    assert "manually" in r.text.lower()


async def test_save_archetype_want_persists_model_specs(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    async with _client(follow=False) as c:
        r = await c.post("/wants", data={
            "name": "4runner platform",
            "archetype_text": "cheap reliable 4runner-platform offroad",
            "spec_make": "Lexus", "spec_model": "GX 470",
            "spec_year_min": "2003", "spec_year_max": "2009", "spec_trims": "",
            "spec_include": "0",
            "max_price_cad": "18000",
        })
    assert r.status_code == 303  # noqa: PLR2004

    wants = await repo.list_wants(session)
    crit = WantCriteria.model_validate(wants[-1].config)
    assert crit.archetype_text == "cheap reliable 4runner-platform offroad"
    assert crit.model_specs == [
        ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009, trims=[])
    ]
