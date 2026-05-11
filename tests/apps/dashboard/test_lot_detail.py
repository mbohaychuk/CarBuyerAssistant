"""Phase 11 Task 46 — lot detail page + comp comparison panel."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.enums import LotStatus
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale


def _seed_lot(session: AsyncSession, *, source_lot_id: str = "L1") -> AuctionLot:
    a = Auction(
        source="hibid",
        source_auction_id="A1",
        url="https://x",
        canonical_url="https://x",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        pickup_province="AB",
        pickup_city="Calgary",
        scheduled_end_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(a)
    lot = AuctionLot(
        auction=a,
        source_lot_id=source_lot_id,
        url=f"https://x/lot/{source_lot_id}",
        title="2010 Ford F-150",
        year=2010,
        make="Ford",
        model="F-150",
        mileage_km=150_000,
        current_high_bid_cad=Decimal("8000"),
        lot_status=LotStatus.OPEN.value,
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
async def test_lot_detail_renders(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Ford" in r.text


@pytest.mark.asyncio
async def test_lot_detail_404_when_missing(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/lots/999999")
    assert r.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_lot_comps_returns_fuzzy_when_no_match(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}/comps")
    assert r.status_code == 200  # noqa: PLR2004
    assert "No exact matches" in r.text


@pytest.mark.asyncio
async def test_lot_comps_returns_fuzzy_when_lot_missing_make_model(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    a = Auction(
        source="hibid", source_auction_id="A2", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    lot = AuctionLot(
        auction=a, source_lot_id="L2", url="https://x/lot/L2",
        title="Unknown", lot_status=LotStatus.OPEN.value,
    )
    session.add(lot)
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot.id}/comps")
    assert r.status_code == 200  # noqa: PLR2004
    assert "No exact matches" in r.text


@pytest.mark.asyncio
async def test_lot_comps_includes_sold_match(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    sold = HistoricalSale(
        year=2010, make="Ford", model="F-150",
        mileage_km=140_000,
        final_price_with_premium_cad=Decimal("9500"),
        sale_channel="hibid",
        sale_platform="hibid",
        disappeared_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    session.add(sold)
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot.id}/comps")
    assert r.status_code == 200  # noqa: PLR2004
    assert "$9,500" in r.text
