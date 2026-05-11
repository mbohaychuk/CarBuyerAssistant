"""Phase 11 Task 48 — action endpoints (mark / notes / admin rescore)."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.enums import UserAction, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot


def _seed_lot(session: AsyncSession) -> AuctionLot:
    a = Auction(
        source="hibid", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    lot = AuctionLot(
        auction=a, source_lot_id="L1", url="https://x/lot/L1",
        title="Test", current_high_bid_cad=Decimal("1000"),
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


# ─── /lots/{id}/mark ───


@pytest.mark.asyncio
async def test_mark_endpoint_updates_user_action(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark", data={"action": "interested"},
        )
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(AuctionLot, lot_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.user_action == UserAction.INTERESTED.value


@pytest.mark.asyncio
async def test_mark_endpoint_404_when_lot_missing(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/lots/999999/mark", data={"action": "interested"},
        )
    assert r.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_mark_endpoint_rejects_invalid_action(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark", data={"action": "garbage"},
        )
    assert r.status_code == 422  # noqa: PLR2004


# ─── /lots/{id}/notes ───


@pytest.mark.asyncio
async def test_notes_appends_to_existing(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.notes = "first"
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/notes", data={"note": "second"},
        )
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(AuctionLot, lot_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.notes == "first\nsecond"


@pytest.mark.asyncio
async def test_notes_writes_when_empty(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/notes", data={"note": "hello"},
        )
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(AuctionLot, lot_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.notes == "hello"


@pytest.mark.asyncio
async def test_notes_404_when_lot_missing(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/lots/999999/notes", data={"note": "x"},
        )
    assert r.status_code == 404  # noqa: PLR2004


# ─── /admin/rescore ───


@pytest.mark.asyncio
async def test_rescore_resets_valuation_status(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.valuation_status = ValuationStatus.DONE.value
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/admin/rescore")
    assert r.status_code == 204  # noqa: PLR2004

    statuses = list((await session.execute(
        select(AuctionLot.valuation_status).where(AuctionLot.id == lot_id),
    )).scalars().all())
    assert statuses == [ValuationStatus.PENDING.value]
