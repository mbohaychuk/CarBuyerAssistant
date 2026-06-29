"""Dashboard /health smoke tests (the flipper views — closing/watched/comps/
sold/purchases — were retired in WG5; their tests went with them)."""
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

_HTTP_OK = 200


@pytest.fixture
def _patch_deps(  # pyright: ignore[reportUnusedFunction]
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _seed_auction_with_lot(session: AsyncSession, *, title: str = "2015 Tacoma") -> AuctionLot:
    a = Auction(
        source="hibid", source_auction_id=f"A-{title}", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        pickup_province="AB", pickup_city="Calgary", scheduled_end_at=None,
    )
    session.add(a)
    lot = AuctionLot(
        auction=a, source_lot_id=f"L-{title}", url=f"https://x/lot/{title}",
        title=title, year=2015, make="Toyota", model="Tacoma",
        lot_status=LotStatus.OPEN.value, current_high_bid_cad=Decimal("8000"),
    )
    session.add(lot)
    return lot


@pytest.mark.asyncio
async def test_health_renders_zero_counts(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == _HTTP_OK
    assert "Auctions tracked: 0" in r.text
    assert "Historical sales: 0" in r.text


@pytest.mark.asyncio
async def test_health_counts_seeded_data(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    _seed_auction_with_lot(session, title="OPEN")
    session.add(HistoricalSale(
        year=2010, make="Ford", model="F-150",
        sale_channel="hibid", sale_platform="hibid",
    ))
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == _HTTP_OK
    assert "Auctions tracked: 1" in r.text
    assert "Open lots: 1" in r.text
    assert "Historical sales: 1" in r.text
