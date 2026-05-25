"""Phase 11 Task 46 — lot detail page + comp comparison panel."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.enums import LotStatus, UserAction
from carbuyer.db.lot_state import apply_user_action
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale


def _seed_lot(
    session: AsyncSession,
    *,
    source_lot_id: str = "L1",
    user_action: str | None = None,
    max_bid_cad: Decimal | None = None,
    bid_placed_at: datetime | None = None,
) -> AuctionLot:
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
    if user_action is not None:
        lot.user_action = UserAction(user_action)
    if max_bid_cad is not None:
        lot.max_bid_cad = max_bid_cad
    if bid_placed_at is not None:
        lot.bid_placed_at = bid_placed_at
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
async def test_lot_detail_renders_flag_evidence(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.showstopper_flags = [
        {"flag": "frame_rust", "evidence": "perforated frame rail near cab mount"},
    ]
    lot.red_flags = [
        {"flag": "engine_light", "evidence": "check-engine light on per listing", "weight": 3},
    ]
    lot.green_flags = [
        {"flag": "service_records", "evidence": "binder of receipts included", "weight": 2},
    ]
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "perforated frame rail near cab mount" in r.text
    assert "check-engine light on per listing" in r.text
    assert "binder of receipts included" in r.text


@pytest.mark.asyncio
async def test_lot_detail_renders_listing_description(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.description = "Runs and drives. Some rust on the box.\nSold as-is."
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Listing description" in r.text
    assert "Runs and drives. Some rust on the box." in r.text


@pytest.mark.asyncio
async def test_lot_detail_no_description_section_when_absent(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Listing description" not in r.text


@pytest.mark.asyncio
async def test_lot_detail_sparse_condition_qualifier(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.condition_categorical = "decent"
    lot.condition_inferred_from_sparse_listing = True
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "inferred from a sparse listing" in r.text


@pytest.mark.asyncio
async def test_lot_detail_no_sparse_qualifier_for_confident_condition(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.condition_categorical = "decent"
    lot.condition_inferred_from_sparse_listing = False
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "inferred from a sparse listing" not in r.text


@pytest.mark.asyncio
async def test_lot_detail_renders_analyst_concerns(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.llm_concerns = [
        {"text": "blue smoke on cold start suggests worn valve seals", "severity": "serious"},
        {"text": "aftermarket exhaust may mask a deeper issue", "severity": "minor"},
    ]
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Analyst notes" in r.text
    assert "blue smoke on cold start suggests worn valve seals" in r.text
    assert "aftermarket exhaust may mask a deeper issue" in r.text
    assert "concern--serious" in r.text
    assert "concern--minor" in r.text


@pytest.mark.asyncio
async def test_lot_detail_no_analyst_notes_when_no_concerns(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Analyst notes" not in r.text


@pytest.mark.asyncio
async def test_lot_detail_unverified_mileage_marker(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.mileage_is_verified = False
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "(unverified)" in r.text


@pytest.mark.asyncio
async def test_lot_detail_no_unverified_marker_when_verified(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.mileage_is_verified = True
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "(unverified)" not in r.text


@pytest.mark.asyncio
async def test_lot_detail_no_unverified_marker_when_provenance_unknown(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "(unverified)" not in r.text


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


@pytest.mark.asyncio
async def test_lot_detail_decision_card_shows_max_bid(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(
        session, user_action="bid_placed", max_bid_cad=Decimal("4250"),
        bid_placed_at=datetime.now(UTC),
    )
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/lots/{lot.id}")
    assert r.status_code == 200  # noqa: PLR2004
    # money macro wraps the amount in a span — assert class + amount
    # separately rather than a literal cross-span substring.
    assert "decision-card__max-bid" in r.text
    assert "$4,250" in r.text


@pytest.mark.asyncio
async def test_lot_detail_renders_activity_timeline(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.flush()
    apply_user_action(session, lot, UserAction.INTERESTED, source="dashboard")
    apply_user_action(
        session, lot, UserAction.BID_PLACED,
        max_bid_cad=Decimal("3500"), source="dashboard",
    )
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Activity" in r.text
    assert 'class="activity-timeline"' in r.text
    # Template renders "bid placed" (underscore replaced with space).
    assert "bid placed" in r.text
    assert 'data-state="bid_placed"' in r.text
    assert "$3,500" in r.text
    assert "dashboard" in r.text


@pytest.mark.asyncio
async def test_lot_detail_activity_empty_state(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Activity" in r.text
    assert "No recorded activity" in r.text


@pytest.mark.asyncio
async def test_lot_detail_activity_renders_cleared_state(
    _patch_deps: AsyncSession,
) -> None:
    """Toggle-off (apply_user_action with action=None) writes a history
    row with user_action=NULL. The timeline renders it as 'Cleared'."""
    session = _patch_deps
    lot = _seed_lot(session)
    await session.flush()
    apply_user_action(session, lot, UserAction.INTERESTED, source="dashboard")
    apply_user_action(session, lot, None, source="dashboard")
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/lots/{lot_id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Cleared" in r.text
    assert 'data-state="cleared"' in r.text
