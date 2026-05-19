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
    best_deals,
    closing_buckets,
    dashboard_kpis,
    derive_watched_make_model,
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
    lot_status: str = LotStatus.OPEN.value,
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
        lot_status=lot_status,
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
        user_action=UserAction.PASSED.value,
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


@pytest.mark.asyncio
async def test_dashboard_kpis_best_deal_none_when_no_scored_lots(
    _patch_deps: AsyncSession,
) -> None:
    """best_deal_pct must be None (not 0.0 or a crash) when no open lot has
    a price_deal_score yet — the template branches on None to render '—'."""
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")
    _seed_lot(session, a, source_lot_id="UNSCORED")  # price_deal_score is None
    await session.commit()

    kpis = await dashboard_kpis(session, now=datetime.now(UTC), alerts_total=0)
    assert kpis.best_deal_pct is None


# ── status exclusion (closed / sold / unsold / force_closed must not leak) ──


@pytest.mark.asyncio
async def test_closing_buckets_excludes_closed_statuses(
    _patch_deps: AsyncSession,
) -> None:
    """The four closing buckets must surface only OPEN_STATUSES lots.
    Without this test, dropping the lot_status.in_(OPEN_STATUSES) filter
    ships green because every other test hardcodes lot_status=OPEN."""
    session = _patch_deps
    now = datetime.now(UTC)
    a = _seed_auction(session, source_id="A1")
    for status in (
        LotStatus.CLOSED.value,
        LotStatus.SOLD.value,
        LotStatus.UNSOLD.value,
        LotStatus.FORCE_CLOSED.value,
    ):
        _seed_lot(
            session, a, source_lot_id=f"STATUS_{status}",
            scheduled_end_at=now + timedelta(minutes=5),
            lot_status=status,
        )
    await session.commit()

    buckets = await closing_buckets(session, now=now)
    leaked = [
        item["lot"].source_lot_id
        for item in (buckets.now + buckets.next_2h + buckets.today + buckets.tomorrow)
        if item["lot"].source_lot_id.startswith("STATUS_")
    ]
    assert leaked == []


@pytest.mark.asyncio
async def test_best_deals_excludes_closed_statuses(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")
    _seed_lot(session, a, source_lot_id="OPEN_DEAL",
              price_deal_score=0.4, lot_status=LotStatus.OPEN.value)
    _seed_lot(session, a, source_lot_id="CLOSED_DEAL",
              price_deal_score=0.5, lot_status=LotStatus.CLOSED.value)
    await session.commit()

    deals = await best_deals(session)
    ids = {item["lot"].source_lot_id for item in deals}
    assert "OPEN_DEAL" in ids
    assert "CLOSED_DEAL" not in ids


# ── best_deals: thresholding + ordering + PASSED exclusion ──────


@pytest.mark.asyncio
async def test_best_deals_filters_by_min_score(_patch_deps: AsyncSession) -> None:
    """The 0.10 floor protects the homepage from marginal-deal noise."""
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")
    _seed_lot(session, a, source_lot_id="WEAK", price_deal_score=0.05)
    _seed_lot(session, a, source_lot_id="STRONG", price_deal_score=0.25)
    await session.commit()

    deals = await best_deals(session)
    ids = {item["lot"].source_lot_id for item in deals}
    assert "STRONG" in ids
    assert "WEAK" not in ids


@pytest.mark.asyncio
async def test_best_deals_orders_by_score_desc(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")
    _seed_lot(session, a, source_lot_id="MID", price_deal_score=0.20)
    _seed_lot(session, a, source_lot_id="TOP", price_deal_score=0.45)
    _seed_lot(session, a, source_lot_id="LOW", price_deal_score=0.12)
    await session.commit()

    deals = await best_deals(session)
    ordered = [item["lot"].source_lot_id for item in deals]
    assert ordered == ["TOP", "MID", "LOW"]


@pytest.mark.asyncio
async def test_best_deals_excludes_not_interested(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = _seed_auction(session, source_id="A1")
    _seed_lot(session, a, source_lot_id="PASSED",
              price_deal_score=0.5,
              user_action=UserAction.PASSED.value)
    _seed_lot(session, a, source_lot_id="KEEP", price_deal_score=0.4)
    await session.commit()

    deals = await best_deals(session)
    ids = {item["lot"].source_lot_id for item in deals}
    assert "KEEP" in ids
    assert "PASSED" not in ids


# ── alerts pre-watermark exclusion ──────────────────────────────────────


@pytest.mark.asyncio
async def test_alerts_new_lots_excludes_pre_watermark(
    _patch_deps: AsyncSession,
) -> None:
    """A matching-make/model lot ingested BEFORE the watermark must not
    surface as a "new listing." Without this assertion the created_at >
    since clause could be dropped and the test suite still passes.

    Postgres `now()` (used by TimestampMixin) returns transaction-start
    time, so wall-clock sleeps don't separate two rows inserted in the
    same transaction. We set created_at explicitly on each seeded lot.
    """
    session = _patch_deps
    watermark = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    a = _seed_auction(session, source_id="A1")
    # The interested lot defines the watched (make, model) set.
    interested = _seed_lot(
        session, a, source_lot_id="HIST_F150",
        make="Ford", model="F-150",
        user_action=UserAction.INTERESTED.value,
    )
    interested.created_at = watermark - timedelta(days=10)
    old = _seed_lot(session, a, source_lot_id="OLD_F150",
                    make="Ford", model="F-150")
    old.created_at = watermark - timedelta(days=2)  # before watermark
    new = _seed_lot(session, a, source_lot_id="NEW_F150",
                    make="Ford", model="F-150")
    new.created_at = watermark + timedelta(hours=1)  # after watermark
    await session.commit()

    alerts = await alerts_since(session, since=watermark)
    new_ids = {item["lot"].source_lot_id for item in alerts.new_lots}
    assert "NEW_F150" in new_ids
    assert "OLD_F150" not in new_ids


# ── /lots remount sanity ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_and_lots_both_respond(_patch_deps: AsyncSession) -> None:
    """Cross-check that the today inbox at / and the moved feed at /lots
    both return 200 — guards against accidental router unmount during
    future refactors."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r_today = await client.get("/")
        r_lots = await client.get("/lots")
    assert r_today.status_code == 200  # noqa: PLR2004
    assert r_lots.status_code == 200  # noqa: PLR2004


# ── derive_watched_make_model: PURCHASED exclusion ──────────────────────


@pytest.mark.asyncio
async def test_derive_watched_make_model_excludes_purchased(
    _patch_deps: AsyncSession,
) -> None:
    """A purchased lot's make/model must NOT enter the derived interest set.

    If PURCHASED were included, the user would receive perpetual "new lot
    matching your interests" alerts for makes/models they already bought.
    """
    session = _patch_deps
    a = _seed_auction(session, source_id="A_PURCHASED_EXCL")
    _seed_lot(
        session, a, source_lot_id="INTERESTED_CAMRY",
        make="Toyota", model="Camry",
        user_action=UserAction.INTERESTED.value,
    )
    purchased = _seed_lot(
        session, a, source_lot_id="PURCHASED_CIVIC",
        make="Honda", model="Civic",
        # PURCHASED requires won_at per the purchased_iff_won_at check constraint.
        user_action=UserAction.PURCHASED.value,
    )
    purchased.won_at = datetime.now(UTC)
    await session.commit()

    pairs = await derive_watched_make_model(session)

    assert ("Toyota", "Camry") in pairs
    assert ("Honda", "Civic") not in pairs
