"""Stateless trigger evaluator: maps a LotState snapshot to notification trigger events.

Want-relative alerts (new match, price drop) are handled separately by the
notifier's want-match path. The triggers here are the auction-timing reminders
for lots the user has manually flagged interested/maybe: ``closing_soon`` (the
lot is within the final hour) and ``lot_extended`` (a soft-close pushed the end
time out).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(slots=True, frozen=True)
class LotState:
    lot_id: int
    user_action: str | None
    scheduled_end_at: datetime | None
    lot_status: str | None = None
    closing_notified_at: datetime | None = None
    extended_notified_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TriggerResult:
    trigger: str  # "closing_soon" | "lot_extended"
    reason: str


# Phase 13 H6: closing_soon fires once per watched lot when the lot is within
# this window. Spec calls for T-24h / T-6h / T-1h tiers; this MVP uses a
# single fire at the most-urgent (T-1h) tier to fit the existing single-column
# closing_notified_at schema. Multi-tier delivery is Phase 2 tiered delivery.
_CLOSING_SOON_WINDOW = timedelta(hours=1)
_WATCHED_ACTIONS = frozenset({"interested", "maybe"})
_ACTIVE_LOT_STATUSES = frozenset({"open", "closing_soon", "extended"})


def evaluate_triggers(state: LotState, *, now: datetime) -> list[TriggerResult]:
    out: list[TriggerResult] = []

    if state.user_action == "not_interested":
        return out

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
