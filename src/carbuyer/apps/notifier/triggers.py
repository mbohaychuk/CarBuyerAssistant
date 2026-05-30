"""Stateless trigger evaluator: maps a LotState snapshot to notification trigger events."""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(slots=True, frozen=True)
class LotState:
    lot_id: int
    rarity_score: float | None
    price_deal_score: float | None
    flag_score: int | None
    confidence_bucket: str | None
    has_showstopper: bool
    user_action: str | None
    scheduled_end_at: datetime | None
    early_warning_notified_at: datetime | None
    cheap_notified_at: datetime | None
    last_cheap_score: float | None
    # Phase 13 H6 (Option A): extra signals for closing_soon + lot_extended.
    # Defaulted so existing test fixtures and any future LotState constructor
    # that doesn't care about closing/extended triggers stays compact.
    # bid_trajectory and multi-tier closing_soon (T-24h, T-6h, T-1h) are
    # deferred — bid_trajectory needs a recommended-max-bid baseline pull
    # that's worth its own design pass, and multi-tier closing needs new
    # timestamp columns to track which tier fired.
    lot_status: str | None = None
    closing_notified_at: datetime | None = None
    extended_notified_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TriggerResult:
    trigger: str  # "early_warning" | "going_cheap" | "closing_soon" | "lot_extended"
    reason: str


# Going-cheap thresholds as a function of time-to-close. Tightest window
# first: a lot 30 min from close uses the T-1h threshold, not T-24h. Beyond
# the widest tier going-cheap never fires — a nominal opening bid days out is
# not signal. See docs/specs/2026-05-27-notification-pivot-design.md (PR-1).
GOING_CHEAP_TIERS: tuple[tuple[timedelta, float], ...] = (
    (timedelta(hours=1), 0.15),
    (timedelta(hours=6), 0.30),
    (timedelta(hours=24), 0.50),
)


def cheap_threshold(
    time_to_close: timedelta,
    tiers: tuple[tuple[timedelta, float], ...] = GOING_CHEAP_TIERS,
) -> float | None:
    """Minimum price_deal_score for a going-cheap alert at this time-to-close.

    Returns the threshold of the closest (tightest) tier whose window contains
    time_to_close, or None when the lot is already closed or further out than
    the widest tier.
    """
    if time_to_close < timedelta(0):
        return None
    for window, threshold in tiers:
        if time_to_close <= window:
            return threshold
    return None


# Phase 13 H6: closing_soon fires once per watched lot when the lot is within
# this window. Spec calls for T-24h / T-6h / T-1h tiers; this MVP uses a
# single fire at the most-urgent (T-1h) tier to fit the existing single-column
# closing_notified_at schema. Multi-tier delivery is Phase 14.
_CLOSING_SOON_WINDOW = timedelta(hours=1)
_WATCHED_ACTIONS = frozenset({"interested", "bid_placed", "purchased"})
_ACTIVE_LOT_STATUSES = frozenset({"open", "closing_soon", "extended"})


def evaluate_triggers(
    state: LotState,
    *,
    now: datetime,
    rarity_threshold: float,
    rescore_improvement_threshold: float,
    early_warning_min_hours: int,
    going_cheap_tiers: tuple[tuple[timedelta, float], ...] = GOING_CHEAP_TIERS,
) -> list[TriggerResult]:
    out: list[TriggerResult] = []

    if state.user_action == "passed":
        return out

    # Early-warning (long-lead): a top-rarity car, not yet notified, far enough
    # from close to plan a trip. Tightened in PR-3 to the long-lead threshold +
    # >=7d-to-close (config) so it does not duplicate the T-24h auction digest.
    if (
        state.rarity_score is not None
        and state.rarity_score >= rarity_threshold
        and state.early_warning_notified_at is None
        and state.scheduled_end_at is not None
        and (state.scheduled_end_at - now) >= timedelta(hours=early_warning_min_hours)
    ):
        out.append(TriggerResult("early_warning", f"rarity={state.rarity_score}"))

    if state.user_action == "purchased":
        return out  # never alert on lots we already own

    # Going-cheap: the alert bar drops as close approaches (see cheap_threshold).
    # All quality gates are block-scoped so later triggers (closing_soon,
    # lot_extended) aren't short-circuited. user_action is already known to be
    # one of {interested, bid_placed, None} here — passed/purchased returned
    # above — so no per-user gate is needed.
    if (
        not state.has_showstopper
        and state.confidence_bucket in {"medium", "high"}
        and (state.flag_score or 0) >= -1
        and state.price_deal_score is not None
        and state.scheduled_end_at is not None
    ):
        threshold = cheap_threshold(state.scheduled_end_at - now, going_cheap_tiers)
        if threshold is not None and state.price_deal_score >= threshold:
            should_fire = state.cheap_notified_at is None or (
                state.last_cheap_score is not None
                and (state.price_deal_score - state.last_cheap_score)
                >= rescore_improvement_threshold
            )
            if should_fire:
                out.append(
                    TriggerResult(
                        "going_cheap",
                        f"score={state.price_deal_score} threshold={threshold}",
                    )
                )

    # Closing-soon (Phase 13 H6): watched lots within the closing window
    # haven't been closing-notified yet, lot is still active.
    if (
        state.user_action in _WATCHED_ACTIONS
        and state.closing_notified_at is None
        and state.lot_status in _ACTIVE_LOT_STATUSES
        and state.scheduled_end_at is not None
        and timedelta(0) <= (state.scheduled_end_at - now) <= _CLOSING_SOON_WINDOW
    ):
        mins = int((state.scheduled_end_at - now).total_seconds() / 60)
        out.append(TriggerResult("closing_soon", f"t_minus_min={mins}"))

    # Lot extended (Phase 13 H6): bid-poller flips lot_status to EXTENDED when
    # a soft-close pushes the end past nominal. Fire once per lot lifetime to
    # tell the watcher their planned end-time is wrong now.
    if (
        state.user_action in _WATCHED_ACTIONS
        and state.lot_status == "extended"
        and state.extended_notified_at is None
    ):
        out.append(TriggerResult("lot_extended", "soft_close_extension"))

    return out
