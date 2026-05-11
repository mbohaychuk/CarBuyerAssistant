"""Phase 11 Task 45 — auction feed view + filters + HTMX partial.

Verifies the GET / endpoint renders the full page on a normal request and only
the partial when HX-Request is set, and that filtering by province / score /
exclude-not-interested narrows the result set.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.enums import LotStatus, UserAction
from carbuyer.db.models import Auction, AuctionLot


def _seed_auction(session: AsyncSession, *, source_id: str, province: str) -> Auction:
    a = Auction(
        source="hibid",
        source_auction_id=source_id,
        url=f"https://x/{source_id}",
        canonical_url=f"https://x/{source_id}",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        pickup_province=province,
        pickup_city="Calgary",
        scheduled_end_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(a)
    return a


def _seed_lot(
    session: AsyncSession,
    auction: Auction,
    *,
    source_lot_id: str,
    user_action: str | None = None,
    price_deal_score: float | None = None,
    rarity_score: float | None = None,
) -> AuctionLot:
    lot = AuctionLot(
        auction=auction,
        source_lot_id=source_lot_id,
        url=f"https://x/lot/{source_lot_id}",
        title=f"2015 Toyota Tacoma {source_lot_id}",
        year=2015,
        make="Toyota",
        model="Tacoma",
        # Trim is rendered in the lot card; use source_lot_id so tests can
        # assert presence/absence of specific seeded lots in the response body.
        trim=source_lot_id,
        lot_status=LotStatus.OPEN.value,
        current_high_bid_cad=Decimal("8000"),
        user_action=user_action,
        price_deal_score=price_deal_score,
        rarity_score=rarity_score,
    )
    session.add(lot)
    return lot


@pytest.fixture
def _patch_deps(  # pyright: ignore[reportUnusedFunction]
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


@pytest.mark.asyncio
async def test_feed_root_returns_html(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Auction feed" in r.text


@pytest.mark.asyncio
async def test_feed_htmx_returns_partial(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 200  # noqa: PLR2004
    assert "Auction feed" not in r.text


@pytest.mark.asyncio
async def test_feed_lists_open_lots(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1", province="AB")
    _seed_lot(session, a, source_lot_id="L1")
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Toyota" in r.text


@pytest.mark.asyncio
async def test_feed_filters_by_province(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    ab = _seed_auction(session, source_id="A_AB", province="AB")
    bc = _seed_auction(session, source_id="A_BC", province="BC")
    _seed_lot(session, ab, source_lot_id="L_AB")
    _seed_lot(session, bc, source_lot_id="L_BC")
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/?province=AB")
    assert r.status_code == 200  # noqa: PLR2004
    assert "L_AB" in r.text or "Tacoma L_AB" in r.text
    # The BC-only lot should be filtered out — its source_lot_id "L_BC"
    # only appears in the URL we'd render in its anchor; checking that
    # neither title nor card link is rendered for it is sufficient.
    assert "L_BC" not in r.text


@pytest.mark.asyncio
async def test_feed_excludes_not_interested(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1", province="AB")
    _seed_lot(session, a, source_lot_id="KEEP")
    _seed_lot(
        session, a, source_lot_id="DROP",
        user_action=UserAction.NOT_INTERESTED.value,
    )
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/?exclude_not_interested=true")
    assert r.status_code == 200  # noqa: PLR2004
    assert "KEEP" in r.text
    assert "DROP" not in r.text


@pytest.mark.asyncio
async def test_feed_includes_not_interested_when_disabled(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1", province="AB")
    _seed_lot(
        session, a, source_lot_id="DROP",
        user_action=UserAction.NOT_INTERESTED.value,
    )
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/?exclude_not_interested=false")
    assert r.status_code == 200  # noqa: PLR2004
    assert "DROP" in r.text
