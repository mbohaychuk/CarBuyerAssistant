from datetime import UTC, datetime, timedelta

from carbuyer.apps.notifier.triggers import (
    LotState,
    TriggerResult,
    cheap_threshold,
    evaluate_triggers,
)

NOW = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)

# Shared thresholds used across every evaluate_triggers call.
RARITY_THRESHOLD = 2.0
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
        rescore_improvement_threshold=RESCORE_IMPROVEMENT,
        early_warning_min_hours=EARLY_WARNING_MIN_HOURS,
    )


def test_early_warning_fires() -> None:
    s = _state(rarity_score=2.5)
    out = _run(s)
    assert any(t.trigger == "early_warning" for t in out)


def test_going_cheap_watched_honors_tier_window() -> None:
    """Watched lots no longer fire going_cheap days out — they honor the same
    time-to-close tiers as everything else."""
    far = _state(
        price_deal_score=0.20,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(days=10),
    )
    near = _state(
        price_deal_score=0.20,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(far))
    assert any(t.trigger == "going_cheap" for t in _run(near))


def test_going_cheap_unflagged_honors_tier_window() -> None:
    """Unflagged lots fire only inside a tier window when the deal clears that
    tier's threshold. 0.20 clears the T-1h tier (0.15) but not anything wider."""
    far = _state(price_deal_score=0.20, scheduled_end_at=NOW + timedelta(days=10))
    near = _state(price_deal_score=0.20, scheduled_end_at=NOW + timedelta(minutes=30))
    assert not any(t.trigger == "going_cheap" for t in _run(far))
    assert any(t.trigger == "going_cheap" for t in _run(near))


def test_passed_suppresses() -> None:
    s = _state(rarity_score=3.0, price_deal_score=0.30, user_action="passed")
    out = _run(s)
    assert out == []


def test_showstopper_suppresses_going_cheap() -> None:
    """In-window lot with a great deal but a showstopper flag: no going_cheap."""
    s = _state(
        price_deal_score=0.60,
        has_showstopper=True,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))


def test_early_warning_ignores_showstopper() -> None:
    """Showstopper gates going_cheap, not early_warning — a rare car closing
    far out still earns its lead-time alert."""
    s = _state(
        rarity_score=2.5,
        has_showstopper=True,
        scheduled_end_at=NOW + timedelta(days=10),
    )
    assert any(t.trigger == "early_warning" for t in _run(s))


def test_bad_flag_score_suppresses_going_cheap() -> None:
    s = _state(
        price_deal_score=0.60,
        flag_score=-2,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))


def test_low_confidence_suppresses_going_cheap() -> None:
    s = _state(
        price_deal_score=0.60,
        confidence_bucket="low",
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))


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
        user_action="interested",
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


# ─── Phase 5.1: purchased suppresses going_cheap / closing_soon / lot_extended


def test_going_cheap_suppressed_when_purchased() -> None:
    """A lot the user already bought generates no going_cheap pings, even with
    a screaming deal inside a firing window."""
    s = _state(
        price_deal_score=0.60,
        user_action="purchased",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))


def test_closing_soon_suppressed_when_purchased() -> None:
    """A purchased lot within the closing window must not fire closing_soon."""
    s = _state(
        user_action="purchased",
        scheduled_end_at=NOW + timedelta(minutes=45),
        lot_status="closing_soon",
    )
    out = _run(s)
    assert not any(t.trigger == "closing_soon" for t in out)


def test_lot_extended_suppressed_when_purchased() -> None:
    """A purchased lot that gets soft-closed must not fire lot_extended."""
    s = _state(
        user_action="purchased",
        lot_status="extended",
        scheduled_end_at=NOW + timedelta(minutes=5),
    )
    out = _run(s)
    assert not any(t.trigger == "lot_extended" for t in out)


def test_passed_suppresses_closing_and_extended() -> None:
    """user_action=passed short-circuits at the top of evaluate_triggers —
    must also block closing_soon and lot_extended."""
    s = _state(
        user_action="passed",
        scheduled_end_at=NOW + timedelta(minutes=30),
        lot_status="extended",
    )
    out = _run(s)
    assert out == []


# ─── PR-1: going-cheap tier table ──────────────────────────────────────────


def test_cheap_threshold_tier_boundaries() -> None:
    # (time_to_close, expected threshold). Closest tier wins; beyond the widest
    # tier or already-closed yields None. Literals live in the table, not in
    # comparisons, so they document the intended thresholds without tripping
    # magic-value lint.
    cases: tuple[tuple[timedelta, float | None], ...] = (
        (timedelta(minutes=30), 0.15),       # T-1h tier
        (timedelta(hours=1), 0.15),          # T-1h boundary (inclusive)
        (timedelta(hours=1, minutes=1), 0.30),   # just past T-1h → T-6h
        (timedelta(hours=6), 0.30),          # T-6h boundary
        (timedelta(hours=6, minutes=1), 0.50),   # just past T-6h → T-24h
        (timedelta(hours=24), 0.50),         # T-24h boundary
        (timedelta(hours=24, minutes=1), None),  # beyond widest tier
        (timedelta(days=3), None),
        (timedelta(minutes=-5), None),       # already closed
    )
    for time_to_close, expected in cases:
        assert cheap_threshold(time_to_close) == expected


def test_going_cheap_t24h_needs_screaming_deal() -> None:
    """~20h out (T-24h tier): only a >= 0.50 deal fires."""
    end = NOW + timedelta(hours=20)
    weak = _state(price_deal_score=0.40, user_action="interested", scheduled_end_at=end)
    strong = _state(price_deal_score=0.55, user_action="interested", scheduled_end_at=end)
    assert not any(t.trigger == "going_cheap" for t in _run(weak))
    assert any(t.trigger == "going_cheap" for t in _run(strong))


def test_going_cheap_t6h_needs_solid_deal() -> None:
    """~3h out (T-6h tier): 0.35 fires, 0.20 does not."""
    end = NOW + timedelta(hours=3)
    weak = _state(price_deal_score=0.20, user_action="interested", scheduled_end_at=end)
    ok = _state(price_deal_score=0.35, user_action="interested", scheduled_end_at=end)
    assert not any(t.trigger == "going_cheap" for t in _run(weak))
    assert any(t.trigger == "going_cheap" for t in _run(ok))


def test_going_cheap_too_far_out_never_fires() -> None:
    """Even a perfect deal does not fire beyond the widest tier."""
    s = _state(
        price_deal_score=0.99,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(days=3),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))


def test_going_cheap_rescore_improvement_refires() -> None:
    """An already-notified lot re-fires only when the deal improved past the
    rescore threshold."""
    s = _state(
        price_deal_score=0.30,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
        cheap_notified_at=NOW - timedelta(hours=2),
        last_cheap_score=0.20,  # delta 0.10 >= RESCORE_IMPROVEMENT (0.05)
    )
    assert any(t.trigger == "going_cheap" for t in _run(s))


def test_going_cheap_no_refire_without_improvement() -> None:
    s = _state(
        price_deal_score=0.30,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
        cheap_notified_at=NOW - timedelta(hours=2),
        last_cheap_score=0.29,  # delta 0.01 < RESCORE_IMPROVEMENT (0.05)
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))
