from datetime import UTC, datetime, timedelta

from carbuyer.apps.notifier.triggers import LotState, TriggerResult, evaluate_triggers

NOW = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)

# Shared thresholds used across every evaluate_triggers call.
RARITY_THRESHOLD = 2.0
NOTIFY_THRESHOLD = 0.15
RESCORE_IMPROVEMENT = 0.05
EARLY_WARNING_MIN_HOURS = 48


def _state(
    *,
    lot_id: int = 1,
    rarity_score: float | None = None,
    price_deal_score: float | None = None,
    flag_score: int | None = 0,
    confidence_bucket: str | None = "high",
    has_showstopper: bool = False,
    user_action: str | None = None,
    scheduled_end_at: datetime | None = None,
    early_warning_notified_at: datetime | None = None,
    cheap_notified_at: datetime | None = None,
    last_cheap_score: float | None = None,
    lot_status: str | None = "open",
    closing_notified_at: datetime | None = None,
    extended_notified_at: datetime | None = None,
) -> LotState:
    if scheduled_end_at is None:
        scheduled_end_at = NOW + timedelta(days=10)
    return LotState(
        lot_id=lot_id,
        rarity_score=rarity_score,
        price_deal_score=price_deal_score,
        flag_score=flag_score,
        confidence_bucket=confidence_bucket,
        has_showstopper=has_showstopper,
        user_action=user_action,
        scheduled_end_at=scheduled_end_at,
        early_warning_notified_at=early_warning_notified_at,
        cheap_notified_at=cheap_notified_at,
        last_cheap_score=last_cheap_score,
        lot_status=lot_status,
        closing_notified_at=closing_notified_at,
        extended_notified_at=extended_notified_at,
    )


def _run(state: LotState) -> list[TriggerResult]:
    return evaluate_triggers(
        state,
        now=NOW,
        rarity_threshold=RARITY_THRESHOLD,
        notify_threshold=NOTIFY_THRESHOLD,
        rescore_improvement_threshold=RESCORE_IMPROVEMENT,
        early_warning_min_hours=EARLY_WARNING_MIN_HOURS,
    )


def test_early_warning_fires() -> None:
    s = _state(rarity_score=2.5)
    out = _run(s)
    assert any(t.trigger == "early_warning" for t in out)


def test_going_cheap_fires_for_watched_anytime() -> None:
    s = _state(
        price_deal_score=0.20,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(days=10),
    )
    out = _run(s)
    assert any(t.trigger == "going_cheap" for t in out)


def test_going_cheap_for_unflagged_only_when_closing_soon() -> None:
    far = _state(price_deal_score=0.20, scheduled_end_at=NOW + timedelta(days=10))
    near = _state(price_deal_score=0.20, scheduled_end_at=NOW + timedelta(hours=12))
    out_far = _run(far)
    out_near = _run(near)
    assert not any(t.trigger == "going_cheap" for t in out_far)
    assert any(t.trigger == "going_cheap" for t in out_near)


def test_not_interested_suppresses() -> None:
    s = _state(rarity_score=3.0, price_deal_score=0.30, user_action="not_interested")
    out = _run(s)
    assert out == []


def test_showstopper_suppresses_going_cheap_but_allows_early_warning() -> None:
    s = _state(
        rarity_score=2.5,
        price_deal_score=0.20,
        has_showstopper=True,
        user_action="interested",
    )
    out = _run(s)
    triggers = {t.trigger for t in out}
    assert "early_warning" in triggers
    assert "going_cheap" not in triggers


def test_bad_flag_score_suppresses_going_cheap() -> None:
    s = _state(
        price_deal_score=0.20,
        flag_score=-2,
        user_action="interested",
    )
    out = _run(s)
    assert not any(t.trigger == "going_cheap" for t in out)


def test_low_confidence_suppresses_going_cheap() -> None:
    s = _state(
        price_deal_score=0.20,
        confidence_bucket="low",
        user_action="interested",
    )
    out = _run(s)
    assert not any(t.trigger == "going_cheap" for t in out)


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
    """closing_soon is watched-only; unflagged lots ride the going_cheap
    closing-window path instead, not a standalone closing_soon."""
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
    must also block closing_soon and lot_extended."""
    s = _state(
        user_action="not_interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
        lot_status="extended",
    )
    out = _run(s)
    assert out == []
