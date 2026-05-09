from __future__ import annotations

from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import (
    EnrichmentStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import AuctionLot

StatusField = Literal[
    "enrichment_status", "valuation_status", "vision_status", "notification_status",
]

_IN_PROGRESS_BY_FIELD: dict[StatusField, str] = {
    "enrichment_status": EnrichmentStatus.IN_PROGRESS,
    "valuation_status": ValuationStatus.IN_PROGRESS,
    "vision_status": VisionStatus.IN_PROGRESS,
    # NotificationStatus has no IN_PROGRESS — notifier flips PENDING → DONE/SKIPPED.
}


async def claim_pending_ids(
    session: AsyncSession,
    *,
    status_field: StatusField,
    limit: int = 50,
) -> list[int]:
    """Claim up to ``limit`` pending lot ids and flip them to in_progress.

    The 'in_progress' marker is the ownership signal. The row lock is held only
    long enough to do the SELECT FOR UPDATE SKIP LOCKED + UPDATE — the function
    flushes and the caller commits (or auto-commits via session context). Then
    the worker processes each id in a fresh, short transaction. A separate
    watchdog (Phase 2.5) flips 'in_progress' rows older than N minutes back to
    'pending' to recover from worker crashes mid-processing.
    """
    if status_field not in _IN_PROGRESS_BY_FIELD:
        raise ValueError(
            f"{status_field} has no IN_PROGRESS state; "
            "use the terminal-status enum directly (e.g. NotificationStatus.DONE)",
        )
    column = getattr(AuctionLot, status_field)
    select_stmt = (
        select(AuctionLot.id)
        .where(column == "pending")
        .order_by(AuctionLot.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(select_stmt)).scalars().all())
    if not rows:
        return []
    in_progress_value = _IN_PROGRESS_BY_FIELD[status_field]
    update_stmt = (
        update(AuctionLot)
        .where(AuctionLot.id.in_(rows))
        .values({status_field: in_progress_value})
    )
    await session.execute(update_stmt)
    await session.flush()
    return rows


async def select_pending_ids(
    session: AsyncSession,
    *,
    status_field: StatusField,
    limit: int = 1000,
) -> list[int]:
    """Read-only scan of pending ids — no locking, no status mutation.

    Used at listener startup and on reconnect to find rows that NOTIFY-fired
    while the worker was down. Caller dispatches each id (typically by issuing
    a fresh NOTIFY) so the regular processing path picks them up.
    """
    column = getattr(AuctionLot, status_field)
    stmt = (
        select(AuctionLot.id)
        .where(column == "pending")
        .order_by(AuctionLot.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
