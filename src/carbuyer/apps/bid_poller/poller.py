"""Bid-poller worker — Phase 7.

Continuously polls open lots at a tiered cadence determined by time-to-close:
every 30s in the final 10 minutes, 1 min within an hour, 5 min within 2 hours,
15 min within 24 hours, 60 min otherwise.

Pipeline integration:
A bid change is the only signal that re-valuation is needed mid-auction. When
this worker observes a new ``current_high_bid_cad`` it sets the lot's
``valuation_status`` back to ``PENDING`` and emits ``NOTIFY valuation_pending``
so the valuator (LISTEN-only on that channel) recomputes price_deal_score with
the latest bid. The valuator in turn emits ``notification_pending`` if the new
score crosses the threshold, waking the notifier. The bid-poller is therefore
the head of a NOTIFY chain that closes the loop between live-auction signal
and Discord delivery.

Design decisions:
- HTTP poll_bid() calls happen OUTSIDE any DB transaction — same principle as
  the enricher (load in short tx → close → I/O → reopen short tx → write).
  Holding a transaction open across network I/O risks idle_in_transaction_session
  timeout (60s).
- Lots are re-fetched by id in the write transaction because the original
  ORM-loaded objects belong to the closed read session.
- Sequential per-lot processing mirrors the valuator choice — at MVP scale the
  workload is I/O-bound per lot, not parallelism-bound across the batch.
- The source registry (SOURCES) is used directly; HibidSource self-registers at
  import time via register(). We enter each BidPoller via AsyncExitStack so
  _http is initialised before any poll_bid() call.
- No claims or queue mechanism — bid-poller selects open lots on every cycle and
  processes whichever ones fall into the fast/slow bucket for this pass.
- Crash recovery is free: there is no IN_PROGRESS state. A worker that dies
  mid-poll leaves the lot at lot_status=open; the next cycle re-loads and
  re-polls it. The Phase 2.5 watchdog (which sweeps stuck IN_PROGRESS rows
  on the *_status columns) is not relevant to this worker.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from sqlalchemy import select

from carbuyer.apps.bid_poller.scheduler import next_poll_delay
from carbuyer.db.enums import LotStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionBidHistory, AuctionLot
from carbuyer.db.notify import notify
from carbuyer.db.session import get_session
from carbuyer.shared.logging import get_logger
from carbuyer.sources.base import SOURCES, BidObservation, BidPoller, LotRef
from carbuyer.sources.farmauctionguide.source import (
    FarmAuctionGuideSource as _FagSource,  # registers plugin
)
from carbuyer.sources.hibid.source import HibidSource as _HibidSource  # registers plugin
from carbuyer.sources.mcdougall.source import (
    McDougallSource as _McDougallSource,  # registers plugin
)

_REGISTERED_PLUGINS = (_HibidSource.name, _McDougallSource.name, _FagSource.name)

log = get_logger("bid_poller")

_BATCH_LIMIT = 200
_FAST_BUCKET_CUTOFF_SECONDS = 300
_FAST_BUCKET_CAP = 20
_SLOW_BUCKET_CAP = 50
_CYCLE_SLEEP_SECONDS = 30
# Hard cap on how long a lot can keep its OPEN/CLOSING_SOON/EXTENDED status past
# its scheduled end. Beyond this the source is unreachable or buggy; better to
# stop spending fast-bucket slots on it than to poll forever every 30s. Phase 7
# overlay #16's "scaling cliff" eats this without the guard.
_MAX_OPEN_PAST_END_SECONDS = 24 * 3600
# Clock-skew ceiling: a gap larger than this means either the source pushed
# bogus end times or our clock has drifted. Force-closing on bad time data could
# wipe legitimate live auctions, so we refuse to act and let the warning surface
# in the journal for operator investigation.
_MAX_FORCE_CLOSE_AGE_SECONDS = 7 * 24 * 3600


def _build_pollers() -> dict[str, BidPoller]:
    """Collect all registered BidPoller sources by name.

    HibidSource self-registers at import via register(); importing this module
    is enough to populate SOURCES["hibid"]. Filter to BidPoller instances only
    so a future listing-only source doesn't accidentally land here.
    """
    return {name: s for name, s in SOURCES.items() if isinstance(s, BidPoller)}


async def _load_open_lot_refs(
    now: datetime,
) -> tuple[list[tuple[int, LotRef]], list[tuple[int, LotRef]]]:
    """Return (fast_bucket, slow_bucket) of (lot_id, LotRef) pairs.

    A single short read transaction loads all open lots and the auction data
    needed to build each LotRef and compute the scheduling bucket. The
    transaction closes before any HTTP I/O.
    """
    async with get_session() as s, s.begin():
        stmt = (
            select(AuctionLot, Auction)
            .join(Auction, Auction.id == AuctionLot.auction_id)
            .where(
                AuctionLot.lot_status.in_(
                    [
                        LotStatus.OPEN,
                        LotStatus.CLOSING_SOON,
                        LotStatus.EXTENDED,
                    ]
                )
            )
            .order_by(Auction.scheduled_end_at.asc().nulls_last())
            .limit(_BATCH_LIMIT)
        )
        rows = (await s.execute(stmt)).all()

        fast: list[tuple[int, LotRef]] = []
        slow: list[tuple[int, LotRef]] = []

        for lot, auction in rows:
            # Drop lots stuck OPEN/CLOSING_SOON/EXTENDED far past their
            # scheduled end. The source's poll_bid should have returned
            # "missing" or "closed" by now; if it hasn't (404 raised before
            # the missing branch, source returning bogus data, etc.) we'd
            # poll forever at 30s. Flip to FORCE_CLOSED out-of-band so the
            # next claim_pending_ids snapshot excludes it.
            #
            # Two thresholds bracket the action:
            #   gap > _MAX_OPEN_PAST_END_SECONDS (24h)   → force-close
            #   gap > _MAX_FORCE_CLOSE_AGE_SECONDS (7d)  → skip + warn
            # The ceiling protects against clock drift wiping a whole batch
            # of live auctions if NTP misbehaves.
            end = auction.scheduled_end_at
            if end is not None:
                age = (now - end).total_seconds()
                if age > _MAX_FORCE_CLOSE_AGE_SECONDS:
                    log.error(
                        "lot age exceeds clock-skew ceiling; refusing"
                        " to force-close (clock or source data suspect)",
                        lot_id=lot.id,
                        source=auction.source,
                        scheduled_end_at=end.isoformat(),
                        age_days=age / 86400,
                    )
                    continue
                if age > _MAX_OPEN_PAST_END_SECONDS:
                    log.warning(
                        "lot stale past end; force-closing",
                        lot_id=lot.id,
                        source=auction.source,
                        scheduled_end_at=end.isoformat(),
                        age_hours=age / 3600,
                    )
                    lot.lot_status = LotStatus.FORCE_CLOSED
                    lot.closed_at = now
                    lot.final_bid_cad = lot.current_high_bid_cad
                    continue
            delay = next_poll_delay(
                scheduled_end=auction.scheduled_end_at,
                now=now,
                status=lot.lot_status,
            )
            ref = LotRef(
                source=auction.source,
                source_auction_id=auction.source_auction_id,
                source_lot_id=lot.source_lot_id,
                url=lot.url,
            )
            if delay.total_seconds() <= _FAST_BUCKET_CUTOFF_SECONDS:
                fast.append((lot.id, ref))
            else:
                slow.append((lot.id, ref))

    return fast, slow


async def _write_observation(lot_id: int, obs: BidObservation) -> None:
    """Apply a BidObservation to the lot row in a fresh write transaction.

    Re-fetches lot and auction by id — the original ORM objects belong to the
    closed read session.
    """
    async with get_session() as s, s.begin():
        lot = await s.get(AuctionLot, lot_id)
        if lot is None:
            return
        auction = await s.get(Auction, lot.auction_id)
        if auction is None:
            return

        history = AuctionBidHistory(
            lot_id=lot.id,
            observed_at=obs.observed_at,
            current_high_bid_cad=obs.current_high_bid_cad,
            end_time_at_observation=obs.end_time_at_observation,
            status_at_observation=obs.status_at_observation,
        )
        s.add(history)

        if obs.current_high_bid_cad is not None:
            bid_changed = lot.current_high_bid_cad != obs.current_high_bid_cad
            lot.current_high_bid_cad = obs.current_high_bid_cad
            lot.last_bid_observed_at = obs.observed_at
            if bid_changed:
                lot.valuation_status = ValuationStatus.PENDING
                await notify(s, "valuation_pending", str(lot.id))

        if obs.end_time_at_observation is not None:
            if (
                auction.scheduled_end_at is not None
                and obs.end_time_at_observation > auction.scheduled_end_at
            ):
                lot.lot_status = LotStatus.EXTENDED
            auction.last_seen_end_at = obs.end_time_at_observation

        if obs.status_at_observation == "missing":
            # Lot disappeared from source — treat as closed. Preserve whatever
            # bid was last recorded; we can't know the true final price.
            if lot.lot_status != LotStatus.CLOSED:
                lot.lot_status = LotStatus.CLOSED
                lot.closed_at = datetime.now(UTC)
                lot.final_bid_cad = lot.current_high_bid_cad
        elif obs.status_at_observation == "closed":
            # Match the "missing" branch's idempotency — if the filter ever
            # changes, two consecutive "closed" observations should not bump
            # closed_at.
            if lot.lot_status != LotStatus.CLOSED:
                lot.lot_status = LotStatus.CLOSED
                lot.closed_at = datetime.now(UTC)
                lot.final_bid_cad = obs.current_high_bid_cad


async def _poll_one(
    lot_id: int,
    ref: LotRef,
    *,
    pollers: dict[str, BidPoller],
) -> None:
    """Poll one lot: HTTP call outside any transaction, then write in a fresh tx."""
    poller = pollers.get(ref.source)
    if poller is None:
        return

    try:
        obs = await poller.poll_bid(ref)
    except Exception:
        log.exception("poll_bid failed", lot_id=lot_id)
        return

    await _write_observation(lot_id, obs)


async def main() -> None:
    """Entry point for the bid-poller worker process.

    Verifies every plugin in _REGISTERED_PLUGINS self-registered at import,
    then enters each BidPoller via AsyncExitStack so HTTP clients are
    initialised. Runs a while-True loop: load open lots into fast/slow buckets,
    poll each, sleep 30s, repeat.
    """
    for name in _REGISTERED_PLUGINS:
        if name not in SOURCES:
            raise RuntimeError(f"plugin {name!r} failed to self-register at import")

    pollers = _build_pollers()
    async with AsyncExitStack() as stack:
        for p in pollers.values():
            await stack.enter_async_context(p)

        while True:
            now = datetime.now(UTC)
            fast, slow = await _load_open_lot_refs(now)

            if len(fast) > _FAST_BUCKET_CAP:
                log.warning(
                    "fast bucket capped",
                    total=len(fast),
                    cap=_FAST_BUCKET_CAP,
                )
            if len(slow) > _SLOW_BUCKET_CAP:
                log.warning(
                    "slow bucket capped",
                    total=len(slow),
                    cap=_SLOW_BUCKET_CAP,
                )

            # Defensive try/except mirrors enricher/valuator/notifier — without it
            # any unhandled exception (DB blip, NOTIFY failure, ...) would
            # propagate out of `while True` and exit the worker process.
            for lot_id, ref in fast[:_FAST_BUCKET_CAP]:
                try:
                    await _poll_one(lot_id, ref, pollers=pollers)
                except Exception:
                    log.exception("poll_one unhandled", lot_id=lot_id)

            for lot_id, ref in slow[:_SLOW_BUCKET_CAP]:
                try:
                    await _poll_one(lot_id, ref, pollers=pollers)
                except Exception:
                    log.exception("poll_one unhandled", lot_id=lot_id)

            log.info(
                "cycle complete",
                fast=min(len(fast), _FAST_BUCKET_CAP),
                slow=min(len(slow), _SLOW_BUCKET_CAP),
            )
            await asyncio.sleep(_CYCLE_SLEEP_SECONDS)
