"""Notifier worker.

Polls ``notification_status='pending'`` rows, evaluates per-lot triggers, and
posts to Discord via direct REST (no gateway connection). All HTTP I/O happens
outside any DB transaction; status writes use short, fresh transactions.

Pattern mirrors enricher.py — claim batch, iterate in fresh sessions, catchup
sweep on startup before LISTEN.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

import aiohttp

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import (
    LotEmbedData,
    render_early_warning_text,
    render_going_cheap_text,
    render_needs_plugin_text,
)
from carbuyer.apps.notifier.discord_post import post_message, post_simple_message
from carbuyer.apps.notifier.triggers import LotState, TriggerResult, evaluate_triggers
from carbuyer.db.enums import NotificationStatus
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import listen
from carbuyer.db.queue import claim_pending_lots, select_pending_ids
from carbuyer.db.session import get_session, get_session_maker
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("notifier")

# How many lots to claim per batch.
_BATCH_SIZE = 50


def _state_from_lot(lot: AuctionLot, auction: Auction) -> LotState:
    return LotState(
        lot_id=lot.id,
        rarity_score=lot.rarity_score,
        price_deal_score=lot.price_deal_score,
        flag_score=lot.flag_score,
        confidence_bucket=lot.confidence_bucket,
        has_showstopper=bool(lot.showstopper_flags),
        user_action=lot.user_action,
        scheduled_end_at=auction.scheduled_end_at,
        early_warning_notified_at=lot.early_warning_notified_at,
        cheap_notified_at=lot.cheap_notified_at,
        # No DB column yet — rescore path inactive until a future phase adds it.
        last_cheap_score=None,
    )


def _embed_data(lot: AuctionLot, auction: Auction) -> LotEmbedData:
    location = ", ".join(filter(None, [auction.pickup_city, auction.pickup_province])) or "?"
    return LotEmbedData(
        lot_id=lot.id,
        url=lot.url,
        title=lot.title or "",
        year=lot.year,
        make=lot.make,
        model=lot.model,
        trim=lot.trim,
        location=location,
        current_high_bid_cad=lot.current_high_bid_cad,
        all_in_cad=lot.all_in_at_current_bid_cad,
        expected_value_cad=lot.expected_value_cad,
        value_low_cad=lot.value_low_cad,
        value_high_cad=lot.value_high_cad,
        price_deal_score=lot.price_deal_score,
        rarity_score=lot.rarity_score,
        confidence_bucket=lot.confidence_bucket,
        condition_categorical=lot.condition_categorical,
        top_red_flags=tuple([f.get("flag", "") for f in (lot.red_flags or [])][:3]),
        top_green_flags=tuple(
            (lot.desirability_signals or [])[:3]
            or [f.get("flag", "") for f in (lot.green_flags or [])][:3]
        ),
        suspicious_underprice=lot.suspicious_underprice_flag,
        scheduled_end_at=auction.scheduled_end_at,
    )


def _render(trigger: TriggerResult, data: LotEmbedData) -> str:
    if trigger.trigger == "early_warning":
        return render_early_warning_text(data)
    if trigger.trigger == "going_cheap":
        return render_going_cheap_text(data)
    # Unrecognised trigger — fall back to a minimal message.
    return f"Lot {data.lot_id}: {trigger.trigger} — {trigger.reason}"


def _timestamp_field_for_trigger(trigger: str) -> str | None:
    """Map trigger name to the AuctionLot timestamp column it stamps."""
    return {
        "early_warning": "early_warning_notified_at",
        "going_cheap": "cheap_notified_at",
    }.get(trigger)


async def _process_one(lot_id: int, *, http_session: aiohttp.ClientSession) -> str:  # noqa: PLR0912
    """Process one claimed lot end-to-end.

    Returns:
      - ``"done"`` — triggers evaluated, posts attempted, status DONE.
      - ``"skipped"`` — no triggers fired, status SKIPPED.
      - ``"missing"`` — lot row vanished between claim and load.
    """
    # Load lot + auction in a short read transaction.
    async with get_session() as s:
        lot = await s.get(AuctionLot, lot_id)
        if lot is None:
            log.warning("lot disappeared between claim and load", lot_id=lot_id)
            return "missing"
        auction = await s.get(Auction, lot.auction_id)
        if auction is None:
            log.warning("auction missing for lot", lot_id=lot_id)
            return "missing"

    state = _state_from_lot(lot, auction)
    now = datetime.now(UTC)
    triggers = evaluate_triggers(
        state,
        now=now,
        rarity_threshold=settings.early_warning_rarity_threshold,
        notify_threshold=settings.notify_threshold,
        rescore_improvement_threshold=settings.rescore_improvement_threshold,
        early_warning_min_hours=settings.early_warning_min_hours_to_close,
    )

    if not triggers:
        async with get_session() as s, s.begin():
            row = await s.get(AuctionLot, lot_id)
            if row is not None:
                row.notification_status = NotificationStatus.SKIPPED
            else:
                log.warning("lot vanished before SKIPPED write", lot_id=lot_id)
        return "skipped"

    data = _embed_data(lot, auction)

    # HTTP I/O outside any DB transaction.
    last_channel: str | None = None
    stamped: dict[str, datetime] = {}
    for trigger in triggers:
        channel_key = select_channel(
            trigger=trigger.trigger, score=lot.price_deal_score,
        )
        channel_id = settings.discord_channels.get(channel_key)
        if channel_id is None:
            log.warning(
                "no channel configured for key",
                channel_key=channel_key,
                lot_id=lot_id,
                trigger=trigger.trigger,
            )
            continue
        content = _render(trigger, data)
        posted = await post_message(channel_id, content, lot_id, session=http_session)
        if posted:
            last_channel = channel_key
            ts_field = _timestamp_field_for_trigger(trigger.trigger)
            if ts_field:
                stamped[ts_field] = now
            log.info(
                "notification posted",
                lot_id=lot_id, trigger=trigger.trigger, channel=channel_key,
            )
        else:
            log.warning(
                "notification post failed",
                lot_id=lot_id, trigger=trigger.trigger, channel_key=channel_key,
            )

    # Write timestamps + status in a fresh short transaction.
    async with get_session() as s, s.begin():
        row = await s.get(AuctionLot, lot_id)
        if row is not None:
            for field, ts in stamped.items():
                setattr(row, field, ts)
            if last_channel is not None:
                row.last_notified_channel = last_channel
            row.notification_status = NotificationStatus.DONE
        else:
            log.error(
                "lot vanished after posts; timestamps lost"
                " — duplicate notification possible on recovery",
                lot_id=lot_id,
            )
    return "done"


async def process_pending(*, http_session: aiohttp.ClientSession) -> int:
    """Claim a batch of pending lots, process each in its own transaction.

    Returns the count of lots claimed (not successes — skips count too).

    Sequential by design — Discord rate limits apply per-bot globally across all
    channels; concurrent posts would race the rate limit instantly.
    """
    sm = get_session_maker()
    async with sm() as claim_session, claim_session.begin():
        lots = await claim_pending_lots(
            claim_session,
            status_field="notification_status",
            limit=_BATCH_SIZE,
        )
    if not lots:
        return 0

    lot_ids = [lot.id for lot in lots]
    for lot_id in lot_ids:
        try:
            await _process_one(lot_id, http_session=http_session)
        except Exception:
            log.exception("process_one unhandled", lot_id=lot_id)
    return len(lot_ids)


async def _catchup_sweep(*, http_session: aiohttp.ClientSession) -> None:
    """Drain rows that were already PENDING when the worker started.

    Every continuous worker runs this before LISTEN to recover NOTIFYs missed
    during downtime (Phase 2 idiom). Phase 13: orphan recovery prepended so a
    prior-crash IN_PROGRESS row doesn't sit forever (the SKIP-LOCKED claim
    only selects PENDING).
    """
    async with get_session() as s, s.begin():
        recovered = await recover_orphans(s, status_field="notification_status")
    if recovered > 0:
        log.warning(
            "recovered orphaned IN_PROGRESS lots at startup",
            count=recovered,
        )
    async with get_session() as s:
        ids = await select_pending_ids(
            s, status_field="notification_status", limit=10_000,
        )
    if not ids:
        log.info("catchup sweep — no pending lots")
        return
    log.info("catchup sweep starting", pending_count=len(ids))
    while True:
        n = await process_pending(http_session=http_session)
        if n == 0:
            break
        log.info("catchup batch processed", count=n)
    log.info("catchup sweep complete")


async def _process_needs_plugin(
    auction_id: int, *, http_session: aiohttp.ClientSession,
) -> None:
    # Catchup is implicit: the auction-discoverer re-fires this NOTIFY on every
    # sweep while needs_plugin_notified_at is NULL, so a missed NOTIFY recovers
    # on the next discovery pass (typically every few minutes / hours).
    async with get_session() as session:
        auction = await session.get(Auction, auction_id)
        if auction is None:
            log.warning("auction disappeared before needs_plugin", auction_id=auction_id)
            return
        if auction.needs_plugin_notified_at is not None:
            return
        if not auction.source.startswith("unknown:"):
            return
        channel_key = select_channel(trigger="needs_plugin", score=None)
        channel_id = settings.discord_channels.get(channel_key)
        if channel_id is None:
            log.warning("no needs_plugin channel configured", channel_key=channel_key)
            return
        # Capture all fields before the HTTP call so we don't hold the session
        # open during network I/O.
        content = render_needs_plugin_text(
            auction_id=auction.id,
            url=auction.url,
            auctioneer_name=auction.auctioneer_name,
            pickup_city=auction.pickup_city,
            pickup_province=auction.pickup_province,
            scheduled_start_at=auction.scheduled_start_at,
        )

    # HTTP I/O outside the DB session.
    posted = await post_simple_message(channel_id, content, session=http_session)
    if not posted:
        return

    # Fresh write transaction to stamp the timestamp.
    async with get_session() as session, session.begin():
        auction = await session.get(Auction, auction_id)
        if auction is None:
            log.warning(
                "auction vanished before needs_plugin stamp",
                auction_id=auction_id,
            )
            return
        auction.needs_plugin_notified_at = datetime.now(UTC)
    log.info(
        "needs_plugin alert posted",
        auction_id=auction_id,
        channel_id=channel_id,
        channel_key=channel_key,
    )


async def _listen_needs_plugin(*, http_session: aiohttp.ClientSession) -> None:
    async for payload in listen("needs_plugin"):
        try:
            auction_id = int(payload)
        except ValueError:
            continue
        try:
            await _process_needs_plugin(auction_id, http_session=http_session)
        except Exception:
            log.exception("needs_plugin processing failed", payload=payload)


async def main() -> None:
    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN not configured")
        sys.exit("DISCORD_BOT_TOKEN not configured")
    async with aiohttp.ClientSession() as http_session:
        await _catchup_sweep(http_session=http_session)
        log.info("notifier starting", listeners=["lot_loop", "needs_plugin_loop"])

        async def _lot_loop() -> None:
            async for _payload in listen("notification_pending"):
                try:
                    await process_pending(http_session=http_session)
                except Exception:
                    log.exception("batch failed; sleeping before next NOTIFY")
                    await asyncio.sleep(5)

        async def _needs_plugin_loop() -> None:
            await _listen_needs_plugin(http_session=http_session)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_lot_loop(), name="lot_loop")
            tg.create_task(_needs_plugin_loop(), name="needs_plugin_loop")
