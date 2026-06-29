"""Valuator worker — Phase 4.

Consumes lots whose enrichment is DONE (via the ``valuation_pending``
NOTIFY channel) and writes the full valuation row: comp count, value range,
expected value, deal score, all-in cost, landed cost, and the
notification-status verdict.

Patterns (mirroring the Phase 3 enricher):
- ``claim_pending_ids`` returns ``list[int]``; per-id work opens its own
  fresh ``get_session()`` with a short transaction.
- ``_catchup_sweep`` drains rows that were PENDING when the worker started.
- ``valuation_attempts`` retry counter — any exception increments and leaves
  PENDING for re-claim until attempts >= ``settings.valuation_max_attempts``,
  then FAILED. We don't classify permanent vs transient: the valuator does no
  network I/O so the failure surface is "DB blip" or "logic bug we'll catch
  in review"; bounded retries handle both.
- Self-NOTIFY ``valuation_pending`` after a batch with any transient leftovers
  so the listener loop drains them without waiting for the next worker
  restart's catchup sweep.
- StrEnum status writes (``ValuationStatus.DONE`` etc.) — never bare strings.

Phase 4 overlay items consumed:
- #8/#9: ``condition_inferred_from_sparse_listing`` threads through
  ``compute_fair_value(..., sparse=...)``.
- #12: showstopper flags OR raw cumulative weight at/below
  ``settings.excessive_red_flag_weight_threshold`` mark
  ``notification_status = SKIPPED``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps._runner import run_worker
from carbuyer.db.enums import NotificationStatus, ValuationStatus
from carbuyer.db.models import (
    Auction,
    AuctionLot,
    PrivateListing,
    VehicleOffer,
    WantMatch,
)
from carbuyer.db.notify import listen, notify
from carbuyer.db.queue import (
    claim_pending_ids,
    recover_orphans,
    select_pending_ids,
)
from carbuyer.db.session import get_session, get_session_maker
from carbuyer.scoring.asking_haircut import effective_acquisition_price
from carbuyer.scoring.comps import build_comp_set
from carbuyer.scoring.fair_value import ConfidenceBucket, compute_fair_value
from carbuyer.scoring.landed_cost import distance_km_between, landed_cost_premium
from carbuyer.scoring.score import (
    all_in_cost,
    cumulative_flag_weight,
    price_deal_score,
)
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock
from carbuyer.wants.service import evaluate_lot_against_wants

log = get_logger("valuator")

# Default rates when the auction row is missing them. AB-typical for the MVP.
DEFAULT_BUYER_PREMIUM_PCT = Decimal("0.10")
DEFAULT_GST_PCT = Decimal("0.05")
DEFAULT_PST_PCT = Decimal("0.00")

# Phase 4 plan: lot.current_high_bid_cad below value_low * this fraction
# trips the "too good to be true" flag (probably a parser bug or an early bid
# we should sanity-check before notifying).
SUSPICIOUS_UNDERPRICE_FRACTION = Decimal("0.85")


def _weights_hash() -> str:
    """Stable short hash of the scoring config. Bumping any of these tunables
    should invalidate previously-computed scores on backfill."""
    payload = json.dumps({
        "scoring_version": settings.scoring_version,
        "excessive_red_flag_weight_threshold": settings.excessive_red_flag_weight_threshold,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _has_required_data(lot: VehicleOffer) -> bool:
    return bool(lot.make and lot.model and lot.year)


def _decide_notification_status(lot: VehicleOffer) -> NotificationStatus:
    """Phase 4 overlay #12: showstoppers always skip notification; cumulative
    raw weight at/below the configured threshold also skips. Otherwise the
    notifier picks the row up via the ``notification_pending`` NOTIFY."""
    if lot.showstopper_flags:
        return NotificationStatus.SKIPPED
    raw_cumulative = cumulative_flag_weight(
        lot.red_flags or [], lot.green_flags or [],
    )
    if raw_cumulative <= settings.excessive_red_flag_weight_threshold:
        return NotificationStatus.SKIPPED
    return NotificationStatus.PENDING


def _apply_pricing(
    lot: AuctionLot, *, auction: Auction, expected_value: Decimal | None,
) -> None:
    """Write the BP/tax/landed-cost-derived fields onto ``lot``.

    Splits out of ``value_one`` so the orchestration stays under ruff's
    statement budget; otherwise this would be inline. Reads ``lot.value_low_cad``
    set by the caller to derive ``suspicious_underprice_flag``.
    """
    bp = auction.buyer_premium_pct or DEFAULT_BUYER_PREMIUM_PCT
    bp_max = auction.buyer_premium_max_cad
    bp_min = auction.buyer_premium_min_cad
    gst = auction.gst_pct or DEFAULT_GST_PCT
    pst = auction.pst_pct or DEFAULT_PST_PCT
    dest_province = auction.pickup_province or settings.home_province
    distance = distance_km_between(settings.home_province, dest_province)
    landed = landed_cost_premium(
        home=settings.home_province, dest=dest_province, distance_km=distance,
    )
    lot.landed_cost_premium_cad = landed

    current_bid = lot.current_high_bid_cad
    if current_bid is not None and expected_value is not None:
        lot.all_in_at_current_bid_cad = all_in_cost(
            current_high_bid=current_bid,
            buyer_premium_pct=bp, gst_pct=gst, pst_pct=pst,
            landed_cost_premium=landed,
            buyer_premium_max_cad=bp_max, buyer_premium_min_cad=bp_min,
        )
        lot.price_deal_score = price_deal_score(
            current_high_bid=current_bid,
            buyer_premium_pct=bp, gst_pct=gst, pst_pct=pst,
            landed_cost_premium=landed,
            expected_value=expected_value,
            buyer_premium_max_cad=bp_max, buyer_premium_min_cad=bp_min,
        )
    else:
        lot.all_in_at_current_bid_cad = None
        lot.price_deal_score = None

    value_low = lot.value_low_cad
    if value_low is not None and current_bid is not None:
        lot.suspicious_underprice_flag = (
            current_bid < (value_low * SUSPICIOUS_UNDERPRICE_FRACTION)
        )
    else:
        lot.suspicious_underprice_flag = False


def _apply_listing_pricing(
    lot: PrivateListing, *, expected_value: Decimal | None,
) -> None:
    """Price a fixed-price private listing: the asking price discounted by the
    §4c asking→sold haircut, with no buyer premium and no GST (private used-car
    sales), plus the province-based landed cost. Reuses ``all_in_cost`` /
    ``price_deal_score`` by passing the haircut-adjusted price as the "bid" and
    zeroing the auction fee/tax inputs.
    """
    dest_province = lot.location_province or settings.home_province
    distance = distance_km_between(settings.home_province, dest_province)
    landed = landed_cost_premium(
        home=settings.home_province, dest=dest_province, distance_km=distance,
    )
    lot.landed_cost_premium_cad = landed

    asking = lot.asking_price_cad
    if asking is not None and expected_value is not None:
        effective = effective_acquisition_price(asking, lot.seller_type)
        zero = Decimal("0")
        lot.all_in_at_current_bid_cad = all_in_cost(
            current_high_bid=effective,
            buyer_premium_pct=zero, gst_pct=zero, pst_pct=zero,
            landed_cost_premium=landed,
        )
        lot.price_deal_score = price_deal_score(
            current_high_bid=effective,
            buyer_premium_pct=zero, gst_pct=zero, pst_pct=zero,
            landed_cost_premium=landed, expected_value=expected_value,
        )
    else:
        lot.all_in_at_current_bid_cad = None
        lot.price_deal_score = None

    value_low = lot.value_low_cad
    if value_low is not None and asking is not None:
        lot.suspicious_underprice_flag = (
            asking < (value_low * SUSPICIOUS_UNDERPRICE_FRACTION)
        )
    else:
        lot.suspicious_underprice_flag = False


async def value_one(session: AsyncSession, lot: VehicleOffer) -> None:
    """Compute and persist the valuation for a single lot.

    Caller controls the transaction. No network I/O happens here — only DB
    reads and ORM mutations. Per Phase 4 overlay #2, if the comp query ever
    grows past ``idle_in_transaction_session_timeout=60s`` we'd split this
    into snapshot → close → compute-in-memory → reopen → write; for MVP scale
    a single-tx pattern is fine and easier to test.
    """
    # Auction lots need their parent auction row for fees/taxes/province;
    # private listings have none. Either way bad-shape rows skip.
    auction = await session.get(Auction, lot.auction_id) if isinstance(lot, AuctionLot) else None
    auction_missing = isinstance(lot, AuctionLot) and auction is None
    if auction_missing or not _has_required_data(lot):
        lot.valuation_status = ValuationStatus.SKIPPED
        # Terminate the row from the notifier's perspective — without this,
        # the lot stays at notification_status=pending forever.
        lot.notification_status = NotificationStatus.SKIPPED
        lot.scoring_version = settings.scoring_version
        return

    assert lot.make is not None and lot.model is not None and lot.year is not None

    comps = await build_comp_set(
        session,
        make=lot.make, model=lot.model, trim=lot.trim,
        year=lot.year, mileage_km=lot.mileage_km or 0,
        exclude_offer_id=lot.id,  # don't let an offer comp against its own row
    )
    fv = compute_fair_value(
        comps,
        condition=lot.condition_categorical or "decent",
        sparse=lot.condition_inferred_from_sparse_listing,
    )

    lot.comp_count = fv.comp_count
    lot.value_low_cad = fv.value_low_cad
    lot.value_mid_cad = fv.value_mid_cad
    lot.value_high_cad = fv.value_high_cad
    lot.expected_value_cad = fv.expected_value_cad
    lot.confidence_bucket = fv.confidence.value
    lot.scoring_version = settings.scoring_version
    lot.weights_hash = _weights_hash()
    lot.last_valuation_error = None

    # Channel-specific pricing: auction lots run BP/tax off the auction row;
    # private listings run the asking→sold haircut with no premium and no GST.
    want_province: str | None = None
    if isinstance(lot, AuctionLot):
        assert auction is not None
        _apply_pricing(lot, auction=auction, expected_value=fv.expected_value_cad)
        want_province = auction.pickup_province
    elif isinstance(lot, PrivateListing):
        _apply_listing_pricing(lot, expected_value=fv.expected_value_cad)
        want_province = lot.location_province
    want_price = lot.offer_price  # channel-specific price via the model property

    if fv.confidence == ConfidenceBucket.INSUFFICIENT:
        # Distinguish "we tried, comp set too thin" from "ran the formula".
        # Without enough comps every downstream metric is noise — skip
        # notification rather than spam guesses.
        lot.valuation_status = ValuationStatus.INSUFFICIENT
        lot.notification_status = NotificationStatus.SKIPPED
    else:
        lot.valuation_status = ValuationStatus.DONE
        lot.notification_status = _decide_notification_status(lot)

    # Want-list match: a lot the user explicitly asked for must alert regardless
    # of the system deal filter (it may even be INSUFFICIENT-priced). Force
    # PENDING when ANY want match is un-notified — covers a brand-new match AND
    # a match whose fire-once stamp was cleared by a price drop (re-alert).
    await evaluate_lot_against_wants(
        session,
        lot,
        pickup_province=want_province,
        offer_price_cad=want_price,
    )
    if await _has_unnotified_want_match(session, lot.id):
        lot.notification_status = NotificationStatus.PENDING


async def _has_unnotified_want_match(session: AsyncSession, offer_id: int) -> bool:
    stmt = select(WantMatch.id).where(
        WantMatch.lot_id == offer_id,
        WantMatch.notified_at.is_(None),
        WantMatch.dismissed.is_(False),
    ).limit(1)
    return (await session.execute(stmt)).first() is not None


async def _process_one(lot_id: int) -> str:
    """Process one claimed lot id end-to-end.

    Returns:
      - ``"done"`` — terminal state written (DONE / INSUFFICIENT / SKIPPED)
        and notification_pending NOTIFY emitted iff notification_status=PENDING.
      - ``"transient"`` — exception raised, status returned to PENDING for
        re-claim (until attempts >= max).
      - ``"failed"`` — attempts hit max, status flipped to FAILED.
      - ``"missing"`` — lot row vanished between claim and load.
    """
    try:
        async with get_session() as s, s.begin():
            lot = await s.get(VehicleOffer, lot_id)
            if lot is None:
                return "missing"
            lot.valuation_attempts = (lot.valuation_attempts or 0) + 1
            await value_one(s, lot)
            if lot.notification_status == NotificationStatus.PENDING:
                await notify(s, "notification_pending", str(lot.id))
        return "done"
    except Exception as exc:
        log.exception("valuation failed", lot_id=lot_id)
        async with get_session() as s, s.begin():
            lot = await s.get(VehicleOffer, lot_id)
            if lot is None:
                return "missing"
            lot.valuation_attempts = (lot.valuation_attempts or 0) + 1
            lot.last_valuation_error = f"{type(exc).__name__}: {exc}"[:500]
            if lot.valuation_attempts >= settings.valuation_max_attempts:
                lot.valuation_status = ValuationStatus.FAILED
                return "failed"
            lot.valuation_status = ValuationStatus.PENDING
        return "transient"


async def process_pending() -> int:
    """Claim a batch and process each id sequentially in its own transaction.

    Sequential (not concurrent): valuator workload is DB-bound, not network-
    bound, and parallel SKIP-LOCKED claims across the same pool give us no
    real win at MVP scale. If throughput becomes a problem we add an
    asyncio.Semaphore-bounded gather like the enricher.
    """
    sm = get_session_maker()
    async with sm() as claim_session, claim_session.begin():
        ids = await claim_pending_ids(
            claim_session,
            status_field="valuation_status",
            limit=settings.valuation_batch_size,
        )
    if not ids:
        return 0

    results: list[str] = []
    for lot_id in ids:
        try:
            results.append(await _process_one(lot_id))
        except Exception:
            # _process_one already logs and persists; swallowing here keeps
            # one bad lot from killing the batch.
            log.exception("process_one unhandled", lot_id=lot_id)
            results.append("transient")

    if any(r == "transient" for r in results):
        async with get_session() as s, s.begin():
            await notify(s, "valuation_pending", "")
    return len(ids)


async def _catchup_sweep() -> None:
    """Drain rows that were PENDING when the worker started.

    Phase 2 design overlay #12 / Phase 3 overlay #3: every continuous worker
    must do this before entering ``LISTEN`` to recover NOTIFYs missed during
    downtime. Phase 13: prepend orphan recovery to handle prior-crash
    IN_PROGRESS rows.
    """
    async with get_session() as s, s.begin():
        recovered = await recover_orphans(s, status_field="valuation_status")
    if recovered > 0:
        log.warning(
            "recovered orphaned IN_PROGRESS lots at startup",
            count=recovered,
        )
    async with get_session() as s:
        ids = await select_pending_ids(
            s, status_field="valuation_status", limit=10_000,
        )
    if not ids:
        log.info("catchup sweep — no pending lots")
        return
    log.info("catchup sweep starting", pending_count=len(ids))
    while True:
        n = await process_pending()
        if n == 0:
            break
        log.info("catchup batch processed", count=n)
    log.info("catchup sweep complete")


async def main() -> None:
    lock_conn = await acquire_singleton_lock("valuator")
    try:
        await _catchup_sweep()
        async for _payload in listen("valuation_pending"):
            try:
                await process_pending()
            except Exception:
                log.exception("batch failed; sleeping before next NOTIFY")
                await asyncio.sleep(5)
    finally:
        await lock_conn.close()


if __name__ == "__main__":
    run_worker("valuator", main)
