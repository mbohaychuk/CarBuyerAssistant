from datetime import UTC, datetime, timedelta

from carbuyer.apps.notifier.triggers import LotState, TriggerResult, evaluate_triggers

NOW = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)


def _state(
    *,
    lot_id: int = 1,
    user_action: str | None = None,
    scheduled_end_at: datetime | None = None,
    lot_status: str | None = "open",
    closing_notified_at: datetime | None = None,
    extended_notified_at: datetime | None = None,
) -> LotState:
    if scheduled_end_at is None:
        scheduled_end_at = NOW + timedelta(days=10)
    return LotState(
        lot_id=lot_id,
        user_action=user_action,
        scheduled_end_at=scheduled_end_at,
        lot_status=lot_status,
        closing_notified_at=closing_notified_at,
        extended_notified_at=extended_notified_at,
    )


def _run(state: LotState) -> list[TriggerResult]:
    return evaluate_triggers(state, now=NOW)


# ─── Phase 13 H6: closing_soon ─────────────────────────────────────────────


def test_closing_soon_fires_for_watched_within_1h() -> None:
    """Watched lot closing in 45 min, never closing-notified, lot still
    active — closing_soon fires."""
    s = _state(
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=45),
        lot_status="closing_soon",
    )
    out = _run(s)
    assert any(t.trigger == "closing_soon" for t in out)


def test_closing_soon_silent_for_unwatched_lot() -> None:
    """closing_soon is watched-only — it only fires on lots the user flagged
    interested/maybe, not on every closing lot."""
    s = _state(
        user_action=None,
        scheduled_end_at=NOW + timedelta(minutes=45),
        lot_status="closing_soon",
    )
    out = _run(s)
    assert not any(t.trigger == "closing_soon" for t in out)


def test_closing_soon_silent_beyond_window() -> None:
    """6 hours out is too far for the T-1h tier; this MVP only fires at T-1h."""
    s = _state(
        user_action="interested",
        scheduled_end_at=NOW + timedelta(hours=6),
        lot_status="open",
    )
    out = _run(s)
    assert not any(t.trigger == "closing_soon" for t in out)


def test_closing_soon_silent_after_close() -> None:
    """Lot already closed (scheduled_end_at in the past) → no closing_soon."""
    s = _state(
        user_action="interested",
        scheduled_end_at=NOW - timedelta(minutes=5),
        lot_status="closed",
    )
    out = _run(s)
    assert not any(t.trigger == "closing_soon" for t in out)


def test_closing_soon_dedups_via_timestamp() -> None:
    """Once closing_notified_at is set, no re-fire."""
    s = _state(
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
        lot_status="closing_soon",
        closing_notified_at=NOW - timedelta(minutes=10),
    )
    out = _run(s)
    assert not any(t.trigger == "closing_soon" for t in out)


# ─── Phase 13 H6: lot_extended ─────────────────────────────────────────────


def test_lot_extended_fires_for_watched_on_status_extended() -> None:
    s = _state(
        user_action="maybe",
        lot_status="extended",
        scheduled_end_at=NOW + timedelta(minutes=5),
    )
    out = _run(s)
    assert any(t.trigger == "lot_extended" for t in out)


def test_lot_extended_silent_for_unwatched_lot() -> None:
    s = _state(
        user_action=None,
        lot_status="extended",
    )
    out = _run(s)
    assert not any(t.trigger == "lot_extended" for t in out)


def test_lot_extended_silent_on_non_extended_status() -> None:
    s = _state(
        user_action="interested",
        lot_status="open",
    )
    out = _run(s)
    assert not any(t.trigger == "lot_extended" for t in out)


def test_lot_extended_dedups_via_timestamp() -> None:
    s = _state(
        user_action="interested",
        lot_status="extended",
        extended_notified_at=NOW - timedelta(minutes=2),
    )
    out = _run(s)
    assert not any(t.trigger == "lot_extended" for t in out)


def test_not_interested_suppresses_closing_and_extended() -> None:
    """user_action=not_interested short-circuits at the top of evaluate_triggers —
    must block both closing_soon and lot_extended."""
    s = _state(
        user_action="not_interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
        lot_status="extended",
    )
    out = _run(s)
    assert out == []
