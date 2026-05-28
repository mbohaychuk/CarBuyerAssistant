"""Tests for notifier.py — worker logic against the test DB.

Uses the same _patched_get_session pattern as test_enricher.py: patches
get_session and get_session_maker on the notifier module so fresh sessions
share the test's outer rolled-back transaction.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.notifier import notifier as notifier_mod
from carbuyer.apps.notifier.notifier import (  # pyright: ignore[reportPrivateUsage]
    _embed_data,
    _in_quiet_hours,
    _process_one,
    process_pending,
)
from carbuyer.db.enums import NotificationStatus
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.shared.config import settings as cfg


def _seed_lot(
    session: AsyncSession,
    *,
    price_deal_score: float | None = None,
    rarity_score: float | None = None,
    confidence_bucket: str | None = "high",
    flag_score: int | None = 0,
    user_action: str | None = None,
    scheduled_end_at: datetime | None = None,
    early_warning_notified_at: datetime | None = None,
    cheap_notified_at: datetime | None = None,
    showstopper_flags: list[object] | None = None,
    notification_status: str = "pending",
) -> tuple[Auction, AuctionLot]:
    end = scheduled_end_at or datetime(2026, 6, 10, tzinfo=UTC)
    a = Auction(
        source="test",
        source_auction_id="A1",
        url="https://x",
        canonical_url="https://x",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        scheduled_end_at=end,
        pickup_province="AB",
    )
    session.add(a)
    lot = AuctionLot(
        auction=a,
        source_lot_id="L1",
        url="https://x/lot/1",
        title="2015 Toyota Land Cruiser",
        description="x" * 200,
        price_deal_score=price_deal_score,
        rarity_score=rarity_score,
        confidence_bucket=confidence_bucket,
        flag_score=flag_score,
        user_action=user_action,
        early_warning_notified_at=early_warning_notified_at,
        cheap_notified_at=cheap_notified_at,
        showstopper_flags=showstopper_flags or [],
        notification_status=notification_status,
        # Minimal valuator output so _embed_data doesn't crash.
        all_in_at_current_bid_cad=None,
        expected_value_cad=None,
        value_low_cad=None,
        value_high_cad=None,
    )
    session.add(lot)
    return a, lot


@pytest.fixture
def _patched_get_session(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    """Patch notifier's get_session and get_session_maker to use the test
    connection so uncommitted savepoint data is visible within the test.
    """
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    def fake_get_session_maker() -> object:
        return maker

    monkeypatch.setattr(notifier_mod, "get_session", fake_get_session)
    monkeypatch.setattr(notifier_mod, "get_session_maker", fake_get_session_maker)
    return session


# ─── _process_one ───


@pytest.mark.asyncio
async def test_process_one_no_triggers_marks_skipped(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lot with no trigger conditions → notification_status='skipped'."""
    session = _patched_get_session
    # price_deal_score below every tier threshold, no rarity → no triggers.
    _, lot = _seed_lot(session, price_deal_score=0.05, notification_status="in_progress")
    await session.flush()

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "skipped"
    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_process_one_fires_going_cheap(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lot with high deal score + interested user → going_cheap fires → DONE."""
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
        notification_status="in_progress",
    )
    await session.flush()

    posted_calls: list[tuple[int, str, int]] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted_calls.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "done"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.DONE
    assert lot.cheap_notified_at is not None
    assert lot.last_notified_channel in {"hot_deals", "watchlist"}
    assert len(posted_calls) == 1
    assert posted_calls[0][2] == lot.id


@pytest.mark.asyncio
async def test_process_one_post_failure_keeps_pending_and_increments_attempts(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 13 C2: when every post_message returns False (Discord blip),
    status returns to PENDING with notification_attempts incremented. Locks
    in the fix for 'lot looked DONE in the DB but no Discord message landed'.
    """
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
        notification_status="in_progress",
    )
    await session.flush()

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return False

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "transient"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.PENDING
    assert lot.notification_attempts == 1
    assert lot.last_notification_error is not None
    assert lot.cheap_notified_at is None  # no stamp on failure


@pytest.mark.asyncio
async def test_process_one_post_failure_flips_failed_at_max_attempts(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After settings.notification_max_attempts unsuccessful attempts, the
    lot stops re-queueing and lands FAILED for ops to investigate."""
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
        notification_status="in_progress",
    )
    # Simulate cfg.notification_max_attempts - 1 prior failed attempts.
    lot.notification_attempts = cfg.notification_max_attempts - 1
    await session.flush()

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return False

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "failed"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.FAILED
    assert lot.notification_attempts == cfg.notification_max_attempts


@pytest.mark.asyncio
async def test_process_one_unconfigured_channel_marks_skipped(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every trigger lands on a missing channel — ops config gap, retrying
    won't help. SKIPPED with last_notification_error recorded."""
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
        notification_status="in_progress",
    )
    await session.flush()

    posted_calls: list[object] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted_calls.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "skipped"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.SKIPPED
    assert lot.last_notification_error is not None
    assert not posted_calls
    # Phase 13 review fix #2: no-channel SKIPPED isn't a delivery failure, so
    # the retry counter must not be polluted. Otherwise if ops fixes the
    # channel config and re-queues the lot, it starts closer to FAILED.
    assert lot.notification_attempts == 0


@pytest.mark.asyncio
async def test_process_one_partial_success_marks_done(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two triggers, first succeeds, second fails. Outcome is DONE — at least
    one message landed. going_cheap + closing_soon are the only pair that can
    co-fire on one lot after tiering (both at T-1h)."""
    session = _patched_get_session
    soon_end = datetime.now(UTC) + timedelta(minutes=30)
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,  # going_cheap fires at the T-1h tier (>= 0.15)
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=soon_end,
        notification_status="in_progress",
    )
    lot.lot_status = "closing_soon"  # closing_soon trigger also fires
    await session.flush()

    call_count = {"n": 0}

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        call_count["n"] += 1
        return call_count["n"] == 1  # first succeeds, rest fail

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 2, "watchlist": 3, "auction_closing": 4},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "done"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.DONE


@pytest.mark.asyncio
async def test_process_one_missing_lot_returns_missing(
    _patched_get_session: AsyncSession,
) -> None:
    """A lot_id that doesn't exist returns 'missing' without crashing."""
    http = MagicMock()
    outcome = await _process_one(999_999, http_session=http)
    assert outcome == "missing"


@pytest.mark.asyncio
async def test_process_one_fires_early_warning(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lot with high rarity + close far out → early_warning fires → DONE."""
    session = _patched_get_session
    # rarity_score above threshold (2.0), scheduled_end_at 72 h out (>= 48 h).
    far_end = datetime(2026, 6, 10, tzinfo=UTC)  # well beyond 48 h from test run
    _, lot = _seed_lot(
        session,
        rarity_score=3.0,
        # price_deal_score below every tier threshold, so going_cheap won't fire.
        price_deal_score=0.05,
        confidence_bucket="high",
        flag_score=0,
        user_action=None,
        scheduled_end_at=far_end,
        early_warning_notified_at=None,
        notification_status="in_progress",
    )
    await session.flush()

    posted_calls: list[tuple[int, str, int]] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted_calls.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"early_warning": 12345},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "done"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.DONE
    assert lot.early_warning_notified_at is not None
    assert lot.last_notified_channel == "early_warning"
    _early_warning_channel_id = 12345
    assert len(posted_calls) == 1
    assert posted_calls[0][0] == _early_warning_channel_id  # correct channel id
    assert posted_calls[0][2] == lot.id


# ─── process_pending ───


@pytest.mark.asyncio
async def test_process_pending_claims_and_processes(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: pending lots → claim_pending_lots → _process_one each → DONE."""
    session = _patched_get_session

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002},
    )

    a = Auction(
        source="test", source_auction_id="A2", url="https://y",
        canonical_url="https://y", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
        pickup_province="AB",
    )
    session.add(a)
    await session.flush()

    lots = [
        AuctionLot(
            auction_id=a.id, source_lot_id=f"LP{i}",
            url=f"https://y/lot/{i}", title=f"lot {i}",
            description="x" * 200,
            price_deal_score=0.30,
            confidence_bucket="high",
            flag_score=0,
            user_action="interested",
            notification_status="pending",
        )
        for i in range(3)
    ]
    session.add_all(lots)
    await session.flush()

    http = MagicMock()
    count = await process_pending(http_session=http)
    assert count == 3  # noqa: PLR2004

    for lot in lots:
        await session.refresh(lot)
        assert lot.notification_status == NotificationStatus.DONE


# ─── already-notified guard regression ───


@pytest.mark.asyncio
async def test_process_one_already_notified_early_warning_skips(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When early_warning_notified_at is already set, early_warning must not fire again."""
    session = _patched_get_session
    far_end = datetime(2026, 6, 10, tzinfo=UTC)
    _, lot = _seed_lot(
        session,
        rarity_score=3.0,
        price_deal_score=0.05,
        confidence_bucket="high",
        flag_score=0,
        user_action=None,
        scheduled_end_at=far_end,
        # Pre-stamp: already notified once.
        early_warning_notified_at=datetime.now(UTC),
        notification_status="in_progress",
    )
    await session.flush()

    posted_calls: list[object] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted_calls.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"early_warning": 12345},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)

    # Guard fires → no triggers → SKIPPED, no post.
    assert outcome == "skipped"
    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.SKIPPED
    assert not posted_calls


@pytest.mark.asyncio
async def test_process_one_already_notified_cheap_skips(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cheap_notified_at is already set, going_cheap must not fire again."""
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
        # Pre-stamp: already notified once.
        cheap_notified_at=datetime.now(UTC),
        notification_status="in_progress",
    )
    await session.flush()

    posted_calls: list[object] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted_calls.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)

    assert outcome == "skipped"
    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.SKIPPED
    assert not posted_calls


# ─── _embed_data desirability_signals fallback ───


@pytest.mark.asyncio
async def test_embed_data_desirability_signals_used_when_present(
    _patched_get_session: AsyncSession,
) -> None:
    """desirability_signals takes priority over green_flags for top_green_flags."""
    session = _patched_get_session
    auction, lot = _seed_lot(session)
    lot.desirability_signals = ["original-paint"]
    lot.green_flags = [{"flag": "X"}]
    await session.flush()

    data = _embed_data(lot, auction)
    assert data.top_green_flags == ("original-paint",)


@pytest.mark.asyncio
async def test_embed_data_green_flags_fallback_when_desirability_signals_empty(
    _patched_get_session: AsyncSession,
) -> None:
    """When desirability_signals is empty, green_flags provides top_green_flags."""
    session = _patched_get_session
    auction, lot = _seed_lot(session)
    lot.desirability_signals = []
    lot.green_flags = [{"flag": "sport-tuned suspension"}]
    await session.flush()

    data = _embed_data(lot, auction)
    assert data.top_green_flags == ("sport-tuned suspension",)


# ─── quiet hours (Phase 13 H7) ─────────────────────────────────────────────


def test_in_quiet_hours_wraparound_window() -> None:
    # Window 22..08 (wraparound at midnight)
    base = datetime(2026, 5, 13, tzinfo=UTC)
    assert _in_quiet_hours(base.replace(hour=22), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=23), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=2), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=7), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=8), 22, 8) is False
    assert _in_quiet_hours(base.replace(hour=12), 22, 8) is False
    assert _in_quiet_hours(base.replace(hour=21), 22, 8) is False


def test_in_quiet_hours_non_wraparound() -> None:
    """Window 9..17 (intuitive non-midnight-crossing case): inclusive start,
    exclusive end."""
    base = datetime(2026, 5, 13, tzinfo=UTC)
    assert _in_quiet_hours(base.replace(hour=9), 9, 17) is True
    assert _in_quiet_hours(base.replace(hour=16), 9, 17) is True
    assert _in_quiet_hours(base.replace(hour=17), 9, 17) is False
    assert _in_quiet_hours(base.replace(hour=8), 9, 17) is False


@pytest.mark.asyncio
async def test_process_one_quiet_hours_override_fires_high_score_going_cheap(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """price_deal_score >= quiet_hours_override_score (0.30) overrides quiet
    hours — the post fires immediately."""
    session = _patched_get_session
    near_end = datetime.now(UTC) + timedelta(hours=3)
    _, lot = _seed_lot(
        session,
        price_deal_score=0.40,  # >= override threshold
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=near_end,
        notification_status="in_progress",
    )
    await session.flush()

    monkeypatch.setattr(notifier_mod, "_in_quiet_hours", lambda *_: True)

    posted: list[object] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "done"
    assert posted  # post DID fire


# ─── Phase 13 review: missing-import + uncovered-path regression ────────────


def test_notifier_module_imports_runtime_names() -> None:
    """Both `notify` and `recover_orphans` are referenced inside async
    functions that earlier tests never reached. Python's lazy name resolution
    let them slip through. This module-level attr check is the cheapest gate
    against a recurrence — fails at test-discovery if either import is
    deleted in a future refactor.
    """
    from carbuyer.apps.notifier import notifier as nm  # noqa: PLC0415

    assert callable(nm.notify), "notifier.py must import `notify`"
    assert callable(nm.recover_orphans), "notifier.py must import `recover_orphans`"


@pytest.mark.asyncio
async def test_process_pending_self_notifies_on_transient_failure(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When at least one lot ends `transient`, process_pending must reach the
    self-NOTIFY branch without raising. Historically a NameError lurked there
    because tests only seeded successful posts. The mere absence of an
    exception here is the regression signal — see the smoke test above for
    why we can't monkey-track `notify` directly without masking the bug.
    """
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,  # going_cheap fires
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
        notification_status="pending",
    )
    await session.flush()

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return False  # every post fails → transient → self-NOTIFY branch

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002},
    )

    http = MagicMock()
    count = await process_pending(http_session=http)
    assert count == 1

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.PENDING
    assert lot.notification_attempts == 1


@pytest.mark.asyncio
async def test_catchup_sweep_recovers_orphaned_in_progress(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker crash between SELECT FOR UPDATE SKIP LOCKED and the terminal
    status write leaves the row stuck IN_PROGRESS. _catchup_sweep must flip
    these back to PENDING at startup so they're claimable again. Historically
    `recover_orphans` was called without being imported — this exercises the
    call site so the NameError can't hide.
    """
    from carbuyer.apps.notifier.notifier import (  # noqa: PLC0415
        _catchup_sweep,
    )

    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        price_deal_score=0.05,  # no triggers → SKIPPED post-recovery
        notification_status="in_progress",  # the orphaned state
    )
    await session.flush()

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002},
    )

    http = MagicMock()
    await _catchup_sweep(http_session=http)

    await session.refresh(lot)
    # Recovery + downstream processing both ran; the lot is no longer stuck.
    assert lot.notification_status != NotificationStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_process_one_quiet_hours_override_fires_closing_in_1h(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lot closing within 1h fires regardless of quiet hours (T-1h spec)."""
    session = _patched_get_session
    soon_end = datetime.now(UTC) + timedelta(minutes=30)
    _, lot = _seed_lot(
        session,
        price_deal_score=0.20,  # below override
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=soon_end,
        notification_status="in_progress",
    )
    await session.flush()

    monkeypatch.setattr(notifier_mod, "_in_quiet_hours", lambda *_: True)

    posted: list[object] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 9001, "watchlist": 9002, "auction_closing": 1234},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "done"
    assert posted  # T-1h fires through quiet hours
