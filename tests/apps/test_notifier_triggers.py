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
