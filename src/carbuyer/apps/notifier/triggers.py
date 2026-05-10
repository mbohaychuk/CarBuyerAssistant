from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(slots=True)
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


@dataclass(slots=True)
class TriggerResult:
    trigger: str  # "early_warning" | "going_cheap" | "skip"
    reason: str


def evaluate_triggers(
    state: LotState,
    *,
    now: datetime,
    rarity_threshold: float,
    notify_threshold: float,
    rescore_improvement_threshold: float,
    early_warning_min_hours: int,
) -> list[TriggerResult]:
    out: list[TriggerResult] = []

    if state.user_action == "not_interested":
        return out

    # Early-warning: rare car, not yet notified, closing far enough out to act.
    if (
        state.rarity_score is not None
        and state.rarity_score >= rarity_threshold
        and state.early_warning_notified_at is None
        and state.scheduled_end_at is not None
        and (state.scheduled_end_at - now) >= timedelta(hours=early_warning_min_hours)
    ):
        out.append(TriggerResult("early_warning", f"rarity={state.rarity_score}"))

    # Going-cheap: price looks good; gate on quality signals first.
    if state.has_showstopper:
        return out
    if state.confidence_bucket not in {"medium", "high"}:
        return out
    if (state.flag_score or 0) < -1:
        return out
    if state.price_deal_score is None or state.price_deal_score < notify_threshold:
        return out

    closing_soon = (
        state.scheduled_end_at is not None
        and state.scheduled_end_at - now <= timedelta(hours=24)
    )
    eligible_user = state.user_action in {"interested", "maybe", None}
    fires_for_watched = state.user_action in {"interested", "maybe"}
    fires_for_unflagged = closing_soon

    should_fire = False
    if state.cheap_notified_at is None and (fires_for_watched or fires_for_unflagged):
        should_fire = True
    elif state.last_cheap_score is not None and (
        state.price_deal_score - state.last_cheap_score
    ) >= rescore_improvement_threshold:
        should_fire = True

    if should_fire and eligible_user:
        out.append(TriggerResult("going_cheap", f"score={state.price_deal_score}"))

    return out
