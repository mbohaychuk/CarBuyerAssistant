"""State-machine for AuctionLot.user_action transitions.

Two entry points: apply_user_action (writer) and lot_action_history
(reader). The writer owns the truth-table from the four-state spec —
bid/win field stamping, downgrade guard, audit-log writes. Callers
(dashboard router, Discord bot) commit; the writer only mutates and
stages. The reader returns the audit trail for one lot, newest first.

Lives in db/ (not apps/dashboard/) so the Discord bot can import it
without pulling FastAPI/Jinja2 through the dashboard package.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import UserAction
from carbuyer.db.models import AuctionLot, LotActionHistory

_DOWNGRADE_LOCKED_FROM: frozenset[UserAction] = frozenset({
    UserAction.BID_PLACED, UserAction.PURCHASED,
})
_DOWNGRADE_LOCKED_TO: frozenset[UserAction | None] = frozenset({
    UserAction.INTERESTED, UserAction.PASSED, None,
})


class _AddableSession(Protocol):
    """Structural minimum required of a session by apply_user_action."""

    def add(self, obj: Any, /, *args: Any, **kwargs: Any) -> None: ...


def apply_user_action(
    session: _AddableSession,
    lot: AuctionLot,
    action: UserAction | None,
    *,
    max_bid_cad: Decimal | None = None,
    source: str,
    now: datetime | None = None,
    allow_downgrade: bool = True,
) -> None:
    """Mutate `lot` to reflect `action`, append a LotActionHistory row.

    Caller commits the session. See module docstring + spec for rules.
    """
    when = now or datetime.now(UTC)

    if action == UserAction.BID_PLACED and max_bid_cad is None:
        raise ValueError(
            "BID_PLACED requires max_bid_cad; caller passed None.",
        )

    if (
        not allow_downgrade
        and lot.user_action in _DOWNGRADE_LOCKED_FROM
        and action in _DOWNGRADE_LOCKED_TO
    ):
        raise ValueError(
            f"Refusing downgrade from {lot.user_action!r} to {action!r}: "
            f"allow_downgrade=False (typically a legacy Discord button).",
        )

    prior_action = lot.user_action

    if action is None:
        lot.user_action = None
        lot.max_bid_cad = None
        lot.bid_placed_at = None
        lot.won_at = None
    elif action in {UserAction.INTERESTED, UserAction.PASSED}:
        lot.user_action = action
        lot.max_bid_cad = None
        lot.bid_placed_at = None
        lot.won_at = None
    elif action == UserAction.BID_PLACED:
        lot.user_action = action
        lot.max_bid_cad = max_bid_cad
        if prior_action != UserAction.BID_PLACED:
            lot.bid_placed_at = when
        lot.won_at = None
    elif action == UserAction.PURCHASED:
        lot.user_action = action
        lot.max_bid_cad = None
        lot.bid_placed_at = None
        if prior_action != UserAction.PURCHASED:
            lot.won_at = when

    history_max_bid = (
        max_bid_cad if action == UserAction.BID_PLACED else None
    )
    session.add(
        LotActionHistory(
            lot_id=lot.id,
            user_action=action,
            max_bid_cad=history_max_bid,
            changed_at=when,
            source=source,
        )
    )


async def lot_action_history(
    session: AsyncSession,
    lot_id: int,
) -> Sequence[LotActionHistory]:
    """Return the audit trail for one lot, newest first.

    Reader co-located with apply_user_action (the writer for the same
    table). Uses the (lot_id, changed_at) index for ordered scan.

    Returns [] both for lots with no history AND for nonexistent lot_id —
    callers must verify lot existence separately if they need to distinguish.
    """
    stmt = (
        select(LotActionHistory)
        .where(LotActionHistory.lot_id == lot_id)
        .order_by(LotActionHistory.changed_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()
