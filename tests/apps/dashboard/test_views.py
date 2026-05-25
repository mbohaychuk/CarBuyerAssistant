"""Phase 11 Task 47 — closing/watched/comps/sold/purchases/health smoke tests.

Each route is exercised at least once with the empty-DB happy path; routes with
filtering or YTD logic get a seeded round-trip to confirm the SQL hits and the
template loops over rows.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.apps.dashboard.routers.watched import build_watchlist_buckets
from carbuyer.db.enums import LotStatus, UserAction
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale, Purchase


def _seed_lot(
    session: AsyncSession,
    *,
    user_action: str | None = None,
    source_lot_id: str = "L1",
) -> AuctionLot:
    a = Auction(
        source="hibid", source_auction_id=f"A-{source_lot_id}", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    lot = AuctionLot(
        auction=a, source_lot_id=source_lot_id, url=f"https://x/lot/{source_lot_id}",
        title="Test",
    )
    if user_action is not None:
        lot.user_action = UserAction(user_action)
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


def _seed_auction_with_lot(
    session: AsyncSession,
    *,
    end_at: datetime | None,
    user_action: str | None = None,
    title: str = "2015 Tacoma",
) -> AuctionLot:
    a = Auction(
        source="hibid",
        source_auction_id=f"A-{title}",
        url="https://x",
        canonical_url="https://x",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        pickup_province="AB",
        pickup_city="Calgary",
        scheduled_end_at=end_at,
    )
    session.add(a)
    lot = AuctionLot(
        auction=a,
        source_lot_id=f"L-{title}",
        url=f"https://x/lot/{title}",
        title=title,
        year=2015,
        make="Toyota",
        model="Tacoma",
        trim=title,
        lot_status=LotStatus.OPEN.value,
        current_high_bid_cad=Decimal("8000"),
        user_action=user_action,
    )
    session.add(lot)
    return lot


# ─── /closing ───


@pytest.mark.asyncio
async def test_closing_empty(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/closing")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Closing in next 24h" in r.text
    assert "Nothing closing soon" in r.text


@pytest.mark.asyncio
async def test_closing_includes_lot_within_window(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    soon = datetime.now(UTC) + timedelta(hours=12)
    far = datetime.now(UTC) + timedelta(hours=72)
    _seed_auction_with_lot(session, end_at=soon, title="SOON")
    _seed_auction_with_lot(session, end_at=far, title="FAR")
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/closing?hours=24")
    assert r.status_code == 200  # noqa: PLR2004
    assert "SOON" in r.text
    assert "FAR" not in r.text


# ─── /watched ───


@pytest.mark.asyncio
async def test_watched_returns_four_buckets(_patch_deps: AsyncSession) -> None:
    """All 4 state sections render; each lot appears in its own bucket."""
    session = _patch_deps
    _seed_auction_with_lot(
        session, end_at=None, user_action=UserAction.INTERESTED.value, title="WANT",
    )
    _seed_auction_with_lot(
        session, end_at=None, user_action=UserAction.PASSED.value, title="PASSED-LOT",
    )
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/watched")
    assert r.status_code == 200  # noqa: PLR2004
    body = r.text
    assert 'data-state="interested"' in body
    assert 'data-state="bid_placed"' in body
    assert 'data-state="purchased"' in body
    assert 'data-state="passed"' in body
    assert "WANT" in body
    assert "PASSED-LOT" in body


@pytest.mark.asyncio
async def test_watched_excludes_null_user_action_lots(_patch_deps: AsyncSession) -> None:
    """Lots with NULL user_action don't appear on the watchlist."""
    session = _patch_deps
    _seed_auction_with_lot(
        session, end_at=None, user_action=None, title="UNTAGGED",
    )
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/watched")
    assert r.status_code == 200  # noqa: PLR2004
    assert "UNTAGGED" not in r.text


@pytest.mark.asyncio
async def test_build_watchlist_buckets_groups_by_state(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    a = Auction(
        source="hibid", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    for sa, ua in [
        ("L1", UserAction.INTERESTED),
        ("L2", UserAction.BID_PLACED),
        ("L3", UserAction.PURCHASED),
        ("L4", UserAction.PASSED),
    ]:
        lot = AuctionLot(
            auction=a, source_lot_id=sa, url=f"https://x/{sa}",
            title=sa, user_action=ua,
        )
        if ua == UserAction.BID_PLACED:
            lot.max_bid_cad = Decimal("5000")
            lot.bid_placed_at = datetime.now(UTC)
        if ua == UserAction.PURCHASED:
            lot.won_at = datetime.now(UTC)
        session.add(lot)
    await session.commit()

    buckets = await build_watchlist_buckets(session)
    assert set(buckets) == {
        "interested", "bid_placed", "purchased", "passed",
    }
    assert len(buckets["interested"]) == 1
    assert len(buckets["bid_placed"]) == 1
    assert buckets["interested"][0]["lot"].source_lot_id == "L1"


@pytest.mark.asyncio
async def test_watched_renders_watchlist_board(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    _seed_lot(session, user_action="interested")
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/watched")
    assert r.status_code == 200  # noqa: PLR2004
    assert 'id="watchlist-board"' in r.text
    for label in ("Interested", "Bid placed", "Purchased", "Passed"):
        assert label in r.text


# ─── /comps ───


@pytest.mark.asyncio
async def test_comps_empty_query_renders_form(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/comps")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Type a make/model" in r.text


@pytest.mark.asyncio
async def test_comps_with_make_model_returns_rows(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    sale = HistoricalSale(
        year=2010, make="Ford", model="F-150",
        mileage_km=140_000,
        final_price_with_premium_cad=Decimal("9500"),
        sale_channel="hibid", sale_platform="hibid",
    )
    session.add(sale)
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/comps?make=Ford&model=F-150")
    assert r.status_code == 200  # noqa: PLR2004
    assert "$9,500" in r.text


# ─── /sold ───


@pytest.mark.asyncio
async def test_sold_empty(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/sold")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Recent sold prices" in r.text


@pytest.mark.asyncio
async def test_sold_with_data(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    sale = HistoricalSale(
        year=2018, make="Honda", model="Civic",
        mileage_km=90_000,
        final_price_with_premium_cad=Decimal("12345"),
        sale_channel="hibid", sale_platform="hibid",
    )
    session.add(sale)
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/sold")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Honda" in r.text
    assert "$12,345" in r.text


# ─── /purchases ───


@pytest.mark.asyncio
async def test_purchases_empty(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/purchases")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Purchases" in r.text
    assert "YTD: 0" in r.text


@pytest.mark.asyncio
async def test_purchases_create_round_trip(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False,
    ) as client:
        r = await client.post(
            "/purchases",
            data={
                "purchase_date": "2026-01-15",
                "make": "Mazda",
                "model": "Miata",
                "year": "1995",
                "purchase_price_cad": "5000.00",
                "province_of_purchase": "AB",
            },
        )
        assert r.status_code == 303  # noqa: PLR2004
        listing = await client.get("/purchases")
    assert listing.status_code == 200  # noqa: PLR2004
    assert "Mazda" in listing.text


@pytest.mark.asyncio
async def test_purchases_ytd_curbsider_warning(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    this_year = datetime.now(UTC).year
    for i in range(4):
        session.add(Purchase(
            purchase_date=date(this_year, 1, 1 + i),
            make="X", model="Y", year=2020,
            purchase_price_cad=Decimal("1000"),
            province_of_purchase="AB",
        ))
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/purchases")
    assert r.status_code == 200  # noqa: PLR2004
    assert "curbsider warning threshold" in r.text


# ─── /health ───


@pytest.mark.asyncio
async def test_health_renders_zero_counts(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Auctions tracked: 0" in r.text
    assert "Historical sales: 0" in r.text


@pytest.mark.asyncio
async def test_health_counts_seeded_data(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    _seed_auction_with_lot(session, end_at=None, title="OPEN")
    session.add(HistoricalSale(
        year=2010, make="Ford", model="F-150",
        sale_channel="hibid", sale_platform="hibid",
    ))
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Auctions tracked: 1" in r.text
    assert "Open lots: 1" in r.text
    assert "Historical sales: 1" in r.text
