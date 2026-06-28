from __future__ import annotations

from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import AuctionLot, VehicleOffer

# All status fields readable for catchup sweeps.
StatusField = Literal[
    "enrichment_status", "valuation_status", "vision_status", "notification_status",
]

# Status fields with an IN_PROGRESS state — claimable via two-phase claim.
# NotificationStatus now has IN_PROGRESS so the notifier can use the same
# claim pattern as the other workers.
ClaimableStatusField = Literal[
    "enrichment_status", "valuation_status", "vision_status", "notification_status",
]

_IN_PROGRESS_BY_FIELD: dict[ClaimableStatusField, str] = {
    "enrichment_status": EnrichmentStatus.IN_PROGRESS,
    "valuation_status": ValuationStatus.IN_PROGRESS,
    "vision_status": VisionStatus.IN_PROGRESS,
    "notification_status": NotificationStatus.IN_PROGRESS,
}


async def _mark_in_progress(
    session: AsyncSession,
    *,
    ids: list[int],
    status_field: ClaimableStatusField,
) -> None:
    in_progress_value = _IN_PROGRESS_BY_FIELD[status_field]
    # Status columns live on the vehicle_offer parent; UPDATE there so the
    # bulk write targets the right table (an update through the AuctionLot
    # child mapper would emit UPDATE auction_lot, which lacks these columns).
    update_stmt = (
        update(VehicleOffer)
        .where(VehicleOffer.id.in_(ids))
        .values({status_field: in_progress_value})
    )
    await session.execute(update_stmt)
    await session.flush()


async def claim_pending_ids(
    session: AsyncSession,
    *,
    status_field: ClaimableStatusField,
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
    await _mark_in_progress(session, ids=rows, status_field=status_field)
    return rows


async def claim_pending_lots(
    session: AsyncSession,
    *,
    status_field: ClaimableStatusField,
    limit: int = 50,
) -> list[AuctionLot]:
    """Claim up to ``limit`` pending lots (full ORM rows) and flip them to
    in_progress. SKIP-LOCKED + UPDATE atomicity matches claim_pending_ids;
    this variant returns full rows so callers don't need a second SELECT
    round-trip when the whole row is needed.
    """
    column = getattr(AuctionLot, status_field)
    stmt = (
        select(AuctionLot)
        .where(column == "pending")
        .order_by(AuctionLot.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    lots = list((await session.execute(stmt)).scalars().all())
    if not lots:
        return []
    await _mark_in_progress(session, ids=[lot.id for lot in lots], status_field=status_field)
    return lots


async def recover_orphans(
    session: AsyncSession,
    *,
    status_field: ClaimableStatusField,
) -> int:
    """Flip IN_PROGRESS rows back to PENDING and return the count.

    Phase 13: called from each worker's catchup-sweep before entering LISTEN.
    Workers are single-instance (Phase 7 overlay #12), so any IN_PROGRESS row
    in this worker's status column at startup must be from a prior crash
    between the claim and the terminal status write. Recovery is unconditional
    (no age threshold) because at startup time there is, by construction, no
    other claimer holding the row.

    Safe even if a prior watchdog flips concurrently: SKIP LOCKED in
    claim_pending_ids ensures the next claim's atomicity, and the UPDATE
    here is idempotent.
    """
    column = getattr(VehicleOffer, status_field)
    in_progress_value = _IN_PROGRESS_BY_FIELD[status_field]
    pending_by_field: dict[ClaimableStatusField, str] = {
        "enrichment_status": EnrichmentStatus.PENDING,
        "valuation_status": ValuationStatus.PENDING,
        "vision_status": VisionStatus.PENDING,
        "notification_status": NotificationStatus.PENDING,
    }
    stmt = (
        update(VehicleOffer)
        .where(column == in_progress_value)
        .values({status_field: pending_by_field[status_field]})
    )
    result = await session.execute(stmt)
    # CursorResult.rowcount is int on UPDATE under psycopg.
    return int(getattr(result, "rowcount", 0) or 0)


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
