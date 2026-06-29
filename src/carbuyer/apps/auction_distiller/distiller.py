"""Auction-distiller — nightly cron worker.

Cron-driven nightly batch (not LISTEN-driven, no claim mechanism, single-instance
assumption — same family as bid-poller and vision-batcher). Reads closed lots older
than DISTILL_AGE_DAYS, copies relevant fields into HistoricalSale, deletes the lot
row (cascade-deletes auction_bid_history), and exits.

Watched-lot exception: lots with user_action INTERESTED or MAYBE are retained for
KEEP_NOTIFIED_DAYS so the user can review the outcome before the row disappears.
After that window they are distilled the same as any other closed lot.

Per-lot transaction + try/except: each lot is distilled in its own short transaction.
One bad lot (corrupt VIN, schema validation failure, etc.) logs an error and continues
— it does not roll back the entire batch. Crash recovery is free: the next nightly
run re-selects any lots that still meet the eligibility criteria.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import LotStatus, UserAction
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale
from carbuyer.db.session import get_session
from carbuyer.shared.logging import get_logger

log = get_logger("distiller")

# Algorithmic retention cutoffs — not ops-tunable cost knobs (same precedent as
# _PESSIMISM_* in vision-batcher). Change these with a code deploy.
#
# DISTILL_AGE_DAYS is load-bearing with carbuyer.scoring.comps.RECENT_AUCTION_LOTS_DAYS:
# the comp-builder reads from auction_lots within that window AND from historical_sales
# beyond it. They must stay equal — if DISTILL is shorter, lots vanish from auction_lots
# while the comp window still references them (gap); if DISTILL is longer, the same sale
# appears in both tables (double-counted). Change them together.
DISTILL_AGE_DAYS = 14
KEEP_NOTIFIED_DAYS = 90


def _channel_from(auction: Auction) -> str:
    return f"auction_{auction.auction_subtype}"


async def distill_lot(session: AsyncSession, lot: AuctionLot, auction: Auction) -> None:
    """Copy lot fields into HistoricalSale and delete the lot row.

    Caller controls the transaction — this function is pure mutation, no commit.
    Deleting the lot cascades to auction_bid_history via the FK ondelete=CASCADE.
    """
    final_bid = lot.final_bid_cad
    bp = auction.buyer_premium_pct
    final_with_premium = None
    if final_bid is not None and bp is not None:
        final_with_premium = final_bid * (1 + bp)

    sale = HistoricalSale(
        year=lot.year,
        make=lot.make,
        model=lot.model,
        trim=lot.trim,
        engine=lot.engine,
        transmission=lot.transmission,
        drivetrain=lot.drivetrain,
        mileage_km=lot.mileage_km,
        vin=lot.vin,
        title_status=lot.title_status,
        province_of_origin=lot.province_of_origin,
        condition_categorical=lot.condition_categorical,
        final_listed_price_cad=final_bid,
        days_listed=None,
        buyer_premium_pct_at_sale=bp,
        final_price_with_premium_cad=final_with_premium,
        sale_channel=_channel_from(auction),
        sale_platform=auction.source,
        seller_province=auction.pickup_province,
        seller_city=auction.pickup_city,
        observed_first_at=auction.first_seen_at,
        disappeared_at=lot.closed_at,
        disposition_reason="sold" if final_bid is not None else "unsold",
        was_notified=any(
            ts is not None
            for ts in (
                lot.closing_notified_at,
                lot.extended_notified_at,
            )
        ),
        was_purchased_by_us=lot.was_purchased_by_us,
        notes=lot.notes,
        schema_version=1,
    )
    session.add(sale)
    await session.delete(lot)


async def main(now: datetime | None = None) -> None:
    """Nightly cron entry point: select eligible closed lots, distill, exit.

    The optional ``now`` parameter exists solely for testing — callers can inject
    a fixed timestamp so eligibility windows are deterministic without real-clock
    sleeps or monkeypatching datetime itself.
    """
    if now is None:
        now = datetime.now(UTC)
    log.info("distiller starting", now=now.isoformat())
    cutoff = now - timedelta(days=DISTILL_AGE_DAYS)
    keep_notified_cutoff = now - timedelta(days=KEEP_NOTIFIED_DAYS)

    # Short read tx: collect only IDs so the session closes before iteration.
    # Lots within the keep window for watched (INTERESTED/MAYBE) lots are
    # excluded here in SQL to avoid fetching rows we'll immediately skip.
    async with get_session() as session:
        stmt = select(AuctionLot.id, AuctionLot.auction_id).where(
            AuctionLot.lot_status.in_(
                [
                    LotStatus.CLOSED,
                    LotStatus.SOLD,
                    LotStatus.UNSOLD,
                    LotStatus.FORCE_CLOSED,
                ],
            ),
            AuctionLot.closed_at.is_not(None),
            AuctionLot.closed_at <= cutoff,
            AuctionLot.was_purchased_by_us.is_(False),
            # Keep watched lots within the retention window. The safe positive
            # form avoids NULL-IN pitfalls: include the lot if user_action is
            # NULL (unreviewed), is not a watched status, or is old enough that
            # even watched lots should be distilled. Using NOT IN on a nullable
            # column silently drops rows where user_action IS NULL.
            or_(
                AuctionLot.user_action.is_(None),
                AuctionLot.user_action.not_in([UserAction.INTERESTED, UserAction.MAYBE]),
                AuctionLot.closed_at <= keep_notified_cutoff,
            ),
        )
        rows = (await session.execute(stmt)).all()

    candidate_ids = [(row[0], row[1]) for row in rows]
    log.info("distiller candidates", count=len(candidate_ids))
    if not candidate_ids:
        # Distinct from a "complete with all-zeros counts" line — makes the
        # "ran and had nothing to do" case unambiguous in nightly logs.
        log.info("distiller no eligible lots; exiting")
        return

    counts: dict[str, int] = {"distilled": 0, "missing": 0, "failed": 0}
    for lot_id, auction_id in candidate_ids:
        try:
            async with get_session() as session, session.begin():
                lot = await session.get(AuctionLot, lot_id)
                auction = await session.get(Auction, auction_id)
                if lot is None or auction is None:
                    log.warning(
                        "lot or auction disappeared before distill",
                        lot_id=lot_id,
                        auction_id=auction_id,
                    )
                    counts["missing"] += 1
                    continue
                await distill_lot(session, lot, auction)
            counts["distilled"] += 1
        except Exception:
            log.exception("distill_lot failed", lot_id=lot_id, auction_id=auction_id)
            counts["failed"] += 1

    log.info("distiller complete", **counts)
