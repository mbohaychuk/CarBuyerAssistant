"""Today inbox — homepage triage view.

Verifies the GET / endpoint renders the four-section layout, that the
last_visited_at watermark is read-before-bump, and that the aggregator
queries bin lots correctly across closing buckets / alert categories.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.apps.dashboard.today_queries import (
    alerts_since,
    closing_buckets,
    dashboard_kpis,
    read_and_bump_last_visit,
)
from carbuyer.db.enums import LotStatus, UserAction
from carbuyer.db.models import Auction, AuctionLot, DashboardState


def _seed_auction(
    session: AsyncSession,
    *,
    source_id: str,
    province: str = "AB",
    end_at: datetime | None = None,
) -> Auction:
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
        scheduled_end_at=end_at or (datetime.now(UTC) + timedelta(hours=12)),
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
    showstopper_flags: list[dict[str, Any]] | None = None,
    last_bid_observed_at: datetime | None = None,
    scheduled_end_at: datetime | None = None,
    make: str = "Toyota",
    model: str = "Tacoma",
) -> AuctionLot:
    lot = AuctionLot(
        auction=auction,
        source_lot_id=source_lot_id,
        url=f"https://x/lot/{source_lot_id}",
        title=f"{source_lot_id}",
        year=2015,
        make=make,
        model=model,
        trim=source_lot_id,
        lot_status=LotStatus.OPEN.value,
        current_high_bid_cad=Decimal("8000"),
        user_action=user_action,
        price_deal_score=price_deal_score,
        showstopper_flags=showstopper_flags or [],
        last_bid_observed_at=last_bid_observed_at,
        scheduled_end_at=scheduled_end_at,
    )
    session.add(lot)
    return lot


@pytest.fixture
def _patch_deps(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


# ── route smoke ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_today_route_renders(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/")
    assert r.status_code == 200  # noqa: PLR2004
    # Above-the-fold KPI strip is always rendered (even with empty DB).
    assert "closing now" in r.text
    assert "watching" in r.text
    assert "best deal" in r.text


@pytest.mark.asyncio
async def test_today_route_shows_closing_lot_in_now_bucket(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    now = datetime.now(UTC)
    a = _seed_auction(session, source_id="A1", end_at=now + timedelta(hours=12))
    _seed_lot(
        session, a, source_lot_id="CLOSING_NOW",
        scheduled_end_at=now + timedelta(minutes=5),
    )
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/")
    assert r.status_code == 200  # noqa: PLR2004
    assert "CLOSING_NOW" in r.text


# ── read_and_bump_last_visit ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bump_returns_prior_value_then_advances(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    # Set a known prior timestamp.
    seeded = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    await session.execute(
        DashboardState.__table__.update()
        .where(DashboardState.id == 1)
        .values(last_visited_at=seeded),
    )
    await session.commit()

    prev = await read_and_bump_last_visit(session)
    await session.commit()

    assert prev == seeded
    # After bump, the row should hold a value > seeded (now-ish).
    after = (
        await session.execute(
            select(DashboardState.last_visited_at).where(DashboardState.id == 1),
        )
    ).scalar_one()
    assert after > seeded


# ── closing_buckets ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_closing_buckets_bin_by_time(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    now = datetime.now(UTC)
    a = _seed_auction(session, source_id="A1", end_at=now + timedelta(days=2))
    _seed_lot(session, a, source_lot_id="NOW_5M",
              scheduled_end_at=now + timedelta(minutes=5))
    _seed_lot(session, a, source_lot_id="NEXT_1H",
              scheduled_end_at=now + timedelta(hours=1))
    _seed_lot(session, a, source_lot_id="LATE_TODAY",
              scheduled_end_at=now + timedelta(hours=6))
    _seed_lot(session, a, source_lot_id="TOMORROW",
              scheduled_end_at=now + timedelta(days=1, hours=2))
    # Far-future lot should be omitted from all buckets.
    _seed_lot(session, a, source_lot_id="WAY_OUT",
              scheduled_end_at=now + timedelta(days=10))
    await session.commit()

    buckets = await closing_buckets(session, now=now)

    now_ids = [item["lot"].source_lot_id for item in buckets.now]
    next_ids = [item["lot"].source_lot_id for item in buckets.next_2h]
    today_ids = [item["lot"].source_lot_id for item in buckets.today]
    tomorrow_ids = [item["lot"].source_lot_id for item in buckets.tomorrow]
    all_ids = now_ids + next_ids + today_ids + tomorrow_ids

    assert "NOW_5M" in now_ids
    assert "NEXT_1H" in next_ids
    assert "LATE_TODAY" in today_ids or "LATE_TODAY" in tomorrow_ids
    # TOMORROW lot at +1d2h crosses the local-midnight boundary; depending on
    # current UTC time and Edmonton offset it may land in tomorrow or today.
    # Either is acceptable — what matters is it appears somewhere.
    assert "TOMORROW" in all_ids
    assert "WAY_OUT" not in all_ids


@pytest.mark.asyncio
async def test_closing_buckets_skip_no_end_time(_patch_deps: AsyncSession) -> None:
    """Lots with no end time (neither lot.scheduled_end_at nor auction's)
    can't be binned and should be silently dropped, not crash the page.
    """
    session = _patch_deps
    a = Auction(
        source="hibid",
        source_auction_id="A_NO_END",
        url="https://x/",
        canonical_url="https://x/",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        pickup_province="AB",
        # scheduled_end_at intentionally NULL
    )
    session.add(a)
    _seed_lot(session, a, source_lot_id="NO_END")  # scheduled_end_at NULL too
    await session.commit()

    buckets = await closing_buckets(session, now=datetime.now(UTC))
    all_items = buckets.now + buckets.next_2h + buckets.today + buckets.tomorrow
    assert all(item["lot"].source_lot_id != "NO_END" for item in all_items)


@pytest.mark.asyncio
async def test_closing_buckets_excludes_not_interested(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    now = datetime.now(UTC)
    a = _seed_auction(session, source_id="A1")
    _seed_lot(
        session, a, source_lot_id="PASSED",
        scheduled_end_at=now + timedelta(minutes=10),
        user_action=UserAction.NOT_INTERESTED.value,
    )
    await session.commit()

    buckets = await closing_buckets(session, now=now)
    assert all(
        item["lot"].source_lot_id != "PASSED"
        for item in buckets.now + buckets.next_2h + buckets.today + buckets.tomorrow
    )


# ── alerts_since ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alerts_empty_watched_set_does_not_crash(
    _patch_deps: AsyncSession,
) -> None:
    """No interested-history → empty watched set → no new-lot alerts.
    The SQL `IN ()` empty-set guard is checked here."""
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")
    _seed_lot(session, a, source_lot_id="L1")  # no user_action
    await session.commit()

    alerts = await alerts_since(session, since=datetime(2020, 1, 1, tzinfo=UTC))
    assert alerts.new_lots == []
    # Other categories also empty (no interested lots).
    assert alerts.state_transitions == []
    assert alerts.showstoppers == []


@pytest.mark.asyncio
async def test_alerts_new_lots_match_watched_make_model(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")

    # The user has historically been interested in Ford F-150 → derives the
    # watched set from this row.
    _seed_lot(
        session, a, source_lot_id="HIST_F150",
        make="Ford", model="F-150",
        user_action=UserAction.INTERESTED.value,
    )
    await session.commit()

    # Now seed a fresh post-watermark lot that matches and one that doesn't.
    watermark = datetime.now(UTC) - timedelta(minutes=1)
    _seed_lot(session, a, source_lot_id="NEW_F150", make="Ford", model="F-150")
    _seed_lot(session, a, source_lot_id="NEW_CAMRY", make="Toyota", model="Camry")
    await session.commit()

    alerts = await alerts_since(session, since=watermark)
    new_ids = {item["lot"].source_lot_id for item in alerts.new_lots}
    assert "NEW_F150" in new_ids
    assert "NEW_CAMRY" not in new_ids


@pytest.mark.asyncio
async def test_alerts_bid_moved_on_interested(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")
    now = datetime.now(UTC)
    _seed_lot(
        session, a, source_lot_id="WATCHED",
        user_action=UserAction.INTERESTED.value,
        last_bid_observed_at=now,
    )
    _seed_lot(
        session, a, source_lot_id="WATCHED_STALE",
        user_action=UserAction.INTERESTED.value,
        last_bid_observed_at=now - timedelta(hours=24),
    )
    await session.commit()

    alerts = await alerts_since(session, since=now - timedelta(hours=1))
    moved_ids = {item["lot"].source_lot_id for item in alerts.state_transitions}
    assert "WATCHED" in moved_ids
    assert "WATCHED_STALE" not in moved_ids


@pytest.mark.asyncio
async def test_alerts_showstoppers_on_interested(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")
    _seed_lot(
        session, a, source_lot_id="SHOWSTOP",
        user_action=UserAction.INTERESTED.value,
        showstopper_flags=[{"flag": "frame_rust", "description": "severe"}],
    )
    # Same flag but not marked interested — should not surface.
    _seed_lot(
        session, a, source_lot_id="SHOWSTOP_UNWATCHED",
        showstopper_flags=[{"flag": "frame_rust", "description": "severe"}],
    )
    await session.commit()

    alerts = await alerts_since(session, since=datetime(2020, 1, 1, tzinfo=UTC))
    ss_ids = {item["lot"].source_lot_id for item in alerts.showstoppers}
    assert "SHOWSTOP" in ss_ids
    assert "SHOWSTOP_UNWATCHED" not in ss_ids


# ── KPIs ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_kpis_counts(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    now = datetime.now(UTC)
    a = _seed_auction(session, source_id="A1")
    _seed_lot(session, a, source_lot_id="WATCH1",
              user_action=UserAction.INTERESTED.value)
    _seed_lot(session, a, source_lot_id="WATCH2",
              user_action=UserAction.INTERESTED.value)
    _seed_lot(session, a, source_lot_id="CLOSING",
              scheduled_end_at=now + timedelta(minutes=5))
    _seed_lot(session, a, source_lot_id="BEST",
              price_deal_score=0.42)
    await session.commit()

    kpis = await dashboard_kpis(session, now=now, alerts_total=7)
    assert kpis.watching == 2  # noqa: PLR2004
    assert kpis.closing_now == 1
    assert kpis.alerts == 7  # noqa: PLR2004
    assert kpis.best_deal_pct == 42.0  # noqa: PLR2004
