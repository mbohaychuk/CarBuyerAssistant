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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import aiohttp
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import (
    LotEmbedData,
    render_closing_soon_text,
    render_lot_extended_text,
    render_needs_plugin_text,
    render_want_match_text,
)
from carbuyer.apps.notifier.channel_resolver import resolve_channels
from carbuyer.apps.notifier.discord_post import post_message, post_simple_message
from carbuyer.apps.notifier.triggers import LotState, TriggerResult, evaluate_triggers
from carbuyer.db.enums import NotificationStatus
from carbuyer.db.models import (
    Auction,
    AuctionLot,
    PrivateListing,
    Search,
    VehicleOffer,
    WantMatch,
)
from carbuyer.db.notify import listen, notify
from carbuyer.db.queue import claim_pending_lots, recover_orphans, select_pending_ids
from carbuyer.db.session import get_session, get_session_maker
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock
from carbuyer.wants.criteria import WantCriteria
from carbuyer.wants.deal import WantDeal, score_want_deal
from carbuyer.wants.delivery import delivery_tier
from carbuyer.wants.leverage import buyer_leverage_line
from carbuyer.wants.matcher import matches

log = get_logger("notifier")

# How many lots to claim per batch.
_BATCH_SIZE = 50


@dataclass(frozen=True)
class WantAlert:
    want_match_id: int
    want_name: str
    deal: WantDeal
    tier: str


async def _load_want_alerts(
    session: AsyncSession,
    lot: VehicleOffer,
    *,
    pickup_province: str | None,
    scheduled_end_at: datetime | None,
    now: datetime,
) -> list[WantAlert]:
    """Un-notified, non-dismissed want_matches for this lot, each with its want
    name and a freshly-computed deal breakdown for the message."""
    rows = (
        await session.execute(
            select(WantMatch.id, Search.name, Search.config)
            .join(Search, Search.id == WantMatch.search_id)
            .where(
                WantMatch.lot_id == lot.id,
                WantMatch.notified_at.is_(None),
                WantMatch.dismissed.is_(False),
                Search.enabled.is_(True),  # a muted want must not fire queued matches
            )
        )
    ).all()
    alerts: list[WantAlert] = []
    for want_match_id, want_name, config in rows:
        try:
            criteria = WantCriteria.model_validate(config)
        except ValidationError:
            log.warning("skipping want alert with invalid config", want_match_id=want_match_id)
            continue
        if not matches(
            lot, criteria, pickup_province=pickup_province, offer_price_cad=lot.offer_price,
        ):
            continue
        deal = score_want_deal(lot, criteria, offer_price_cad=lot.offer_price)
        previous_asking = (
            lot.previous_asking_price_cad if isinstance(lot, PrivateListing) else None
        )
        tier = delivery_tier(
            want_relative_score=deal.score,
            offer_price_cad=lot.offer_price,
            previous_asking_price_cad=previous_asking,
            scheduled_end_at=scheduled_end_at,
            now=now,
            deal_threshold=settings.instant_deal_threshold,
            closing_hours=settings.instant_closing_hours,
        )
        alerts.append(WantAlert(want_match_id, want_name, deal, tier))
    return alerts


async def _post_want_alerts(
    want_alerts: list[WantAlert],
    data: LotEmbedData,
    lot_id: int,
    *,
    http_session: aiohttp.ClientSession,
    now: datetime,
) -> tuple[bool, bool, bool, list[int], str | None, str | None]:
    """Post INSTANT-tier want matches to the 'wants' channel, outside quiet hours.
    Digest-tier and quiet-deferred matches are left un-notified for the daily
    digest. Returns (attempted, succeeded, failed, stamped_want_match_ids,
    last_channel, last_error) where `failed` means at least one delivery to a
    configured channel failed."""
    attempted = succeeded = failed = False
    stamped_ids: list[int] = []
    last_channel: str | None = None
    last_error: str | None = None
    quiet = _in_quiet_hours(now, settings.quiet_hours_start, settings.quiet_hours_end)
    for alert in want_alerts:
        if alert.tier != "instant" or quiet:
            continue  # digest-tier + quiet-deferred matches wait for the daily digest
        channel_key = select_channel(trigger="want_match", score=None)
        channel_id = settings.discord_channels.get(channel_key)
        if channel_id is None:
            log.warning(
                "no channel configured for key",
                channel_key=channel_key, lot_id=lot_id, trigger="want_match",
            )
            last_error = f"no_channel:{channel_key}"
            continue
        assert isinstance(channel_id, int), f"unresolved channel {channel_key!r}"
        attempted = True
        content = render_want_match_text(
            data,
            want_name=alert.want_name,
            pct_below_market=alert.deal.score,
            dollars_below_market_cad=alert.deal.dollars_below_market_cad,
            dollars_under_ceiling_cad=alert.deal.dollars_under_ceiling_cad,
            comp_count=alert.deal.comp_count,
        )
        if await post_message(channel_id, content, lot_id, session=http_session):
            succeeded = True
            last_channel = channel_key
            stamped_ids.append(alert.want_match_id)
            log.info(
                "want-match notification posted",
                lot_id=lot_id, want_name=alert.want_name, channel=channel_key,
            )
        else:
            failed = True
            last_error = f"post_failed:{channel_key}:want_match"
            log.warning(
                "want-match post failed",
                lot_id=lot_id, want_name=alert.want_name, channel_key=channel_key,
            )
    return attempted, succeeded, failed, stamped_ids, last_channel, last_error


def _state_from_lot(lot: AuctionLot, auction: Auction) -> LotState:
    return LotState(
        lot_id=lot.id,
        user_action=lot.user_action,
        scheduled_end_at=auction.scheduled_end_at,
        lot_status=lot.lot_status,
        closing_notified_at=lot.closing_notified_at,
        extended_notified_at=lot.extended_notified_at,
    )


def _embed_data(lot: VehicleOffer, auction: Auction | None, now: datetime) -> LotEmbedData:
    previous_asking = None
    if auction is not None:
        location = (
            ", ".join(filter(None, [auction.pickup_city, auction.pickup_province])) or "?"
        )
        end_at = auction.scheduled_end_at
    else:
        location = (lot.location_province if isinstance(lot, PrivateListing) else None) or "?"
        end_at = None
        previous_asking = lot.previous_asking_price_cad if isinstance(lot, PrivateListing) else None
    return LotEmbedData(
        lot_id=lot.id,
        url=lot.url,
        title=lot.title or "",
        year=lot.year,
        make=lot.make,
        model=lot.model,
        trim=lot.trim,
        location=location,
        current_high_bid_cad=lot.offer_price,
        all_in_cad=lot.all_in_at_current_bid_cad,
        value_low_cad=lot.value_low_cad,
        value_high_cad=lot.value_high_cad,
        scheduled_end_at=end_at,
        previous_asking_cad=previous_asking,
        recall_count=lot.recall_count,
        complaint_count=lot.complaint_count,
        leverage_line=buyer_leverage_line(lot, now),
    )


def _render(trigger: TriggerResult, data: LotEmbedData) -> str:
    if trigger.trigger == "closing_soon":
        return render_closing_soon_text(data)
    if trigger.trigger == "lot_extended":
        return render_lot_extended_text(data)
    # Unrecognised trigger — fall back to a minimal message.
    return f"Lot {data.lot_id}: {trigger.trigger} — {trigger.reason}"


def _timestamp_field_for_trigger(trigger: str) -> str | None:
    """Map trigger name to the AuctionLot timestamp column it stamps."""
    return {
        "closing_soon": "closing_notified_at",
        "lot_extended": "extended_notified_at",
    }.get(trigger)


def _in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """Quiet hours window wraps midnight when start > end (typical: 22..08).

    UTC hour-of-day — a single-user MVP; a per-user local offset is a later
    refinement. Only the instant want-alert path consults this; a quiet-window
    instant match is deferred into the next morning digest.
    """
    h = now.hour
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


async def _process_one(  # noqa: PLR0911, PLR0912, PLR0915
    lot_id: int, *, http_session: aiohttp.ClientSession,
) -> str:
    """Process one claimed lot end-to-end.

    Returns:
      - ``"done"`` — at least one configured trigger posted successfully,
        status DONE with timestamps stamped.
      - ``"skipped"`` — no triggers fired (no work to do), status SKIPPED.
      - ``"missing"`` — lot row vanished between claim and load.
      - ``"transient"`` — at least one trigger had a configured channel and
        the post failed. Status returns to PENDING with notification_attempts
        incremented; caller's self-NOTIFY drains the leftover.
      - ``"failed"`` — same as transient but attempts >= max; flips to FAILED.

    Phase 13 fix C2: previously every outcome other than "no triggers" wrote
    notification_status=DONE, so a Discord rate-limit / 4xx / network blip
    silently lost the notification while the DB showed the lot as notified.
    """
    now = datetime.now(UTC)
    # Load offer (+ auction, for auction lots) in a short read transaction.
    async with get_session() as s:
        lot = await s.get(VehicleOffer, lot_id)
        if lot is None:
            log.warning("lot disappeared between claim and load", lot_id=lot_id)
            return "missing"
        auction: Auction | None = None
        if isinstance(lot, AuctionLot):
            auction = await s.get(Auction, lot.auction_id)
            if auction is None:
                log.warning("auction missing for lot", lot_id=lot_id)
                return "missing"
        want_alerts = await _load_want_alerts(
            s, lot,
            pickup_province=(
                auction.pickup_province if auction is not None
                else (lot.location_province if isinstance(lot, PrivateListing) else None)
            ),
            scheduled_end_at=auction.scheduled_end_at if auction is not None else None,
            now=now,
        )

    # Auction triggers (closing_soon, lot_extended) gate on bid state + scheduled
    # close, which private listings don't have — those alert only via want
    # matches. So triggers are auction-only.
    if isinstance(lot, AuctionLot) and auction is not None:
        triggers = evaluate_triggers(_state_from_lot(lot, auction), now=now)
    else:
        triggers = []

    if not triggers and not want_alerts:
        async with get_session() as s, s.begin():
            row = await s.get(VehicleOffer, lot_id)
            if row is not None:
                row.notification_status = NotificationStatus.SKIPPED
            else:
                log.warning("lot vanished before SKIPPED write", lot_id=lot_id)
        return "skipped"

    data = _embed_data(lot, auction, now)

    # HTTP I/O outside any DB transaction.
    last_channel: str | None = None
    stamped: dict[str, datetime] = {}
    any_post_attempted = False
    any_post_succeeded = False
    last_error: str | None = None
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
            last_error = f"no_channel:{channel_key}"
            continue
        # Post-resolution invariant: every value in discord_channels is an
        # int. Names are either resolved at notifier startup or dropped.
        assert isinstance(channel_id, int), f"unresolved channel {channel_key!r}"
        any_post_attempted = True
        content = _render(trigger, data)
        posted = await post_message(channel_id, content, lot_id, session=http_session)
        if posted:
            any_post_succeeded = True
            last_channel = channel_key
            ts_field = _timestamp_field_for_trigger(trigger.trigger)
            if ts_field:
                stamped[ts_field] = now
            log.info(
                "notification posted",
                lot_id=lot_id, trigger=trigger.trigger, channel=channel_key,
            )
        else:
            last_error = f"post_failed:{channel_key}:{trigger.trigger}"
            log.warning(
                "notification post failed",
                lot_id=lot_id, trigger=trigger.trigger, channel_key=channel_key,
            )

    # Want-match alerts (dedup keyed on want_matches.notified_at, not lot columns).
    (
        w_attempted, w_succeeded, w_failed, stamped_want_ids, w_channel, w_error,
    ) = await _post_want_alerts(want_alerts, data, lot_id, http_session=http_session, now=now)
    any_post_attempted = any_post_attempted or w_attempted
    any_post_succeeded = any_post_succeeded or w_succeeded
    if w_channel is not None:
        last_channel = w_channel
    if w_error is not None:
        last_error = w_error

    # Decide outcome based on whether anything reached Discord.
    if any_post_succeeded:
        final = "done"
        async with get_session() as s, s.begin():
            # Stamp the want ledger first — it's independent of the lot row, so a
            # vanished lot can't cause a duplicate want alert on recovery.
            if stamped_want_ids:
                await s.execute(
                    update(WantMatch)
                    .where(WantMatch.id.in_(stamped_want_ids))
                    .values(notified_at=now)
                )
            row = await s.get(VehicleOffer, lot_id)
            if row is None:
                log.error(
                    "lot vanished after posts; timestamps lost"
                    " — duplicate notification possible on recovery",
                    lot_id=lot_id,
                )
                return "done"
            for field, ts in stamped.items():
                setattr(row, field, ts)
            if last_channel is not None:
                row.last_notified_channel = last_channel
            if w_failed:
                # A want post failed amid successes. Want matches don't re-fire on
                # re-enqueue the way per-column triggers do, so keep the lot PENDING
                # to retry the un-stamped want, counting the attempt toward the cap.
                row.last_notification_error = last_error
                row.notification_attempts = (row.notification_attempts or 0) + 1
                if row.notification_attempts >= settings.notification_max_attempts:
                    row.notification_status = NotificationStatus.FAILED
                    final = "failed"
                else:
                    row.notification_status = NotificationStatus.PENDING
                    final = "transient"
            else:
                row.notification_status = NotificationStatus.DONE
                row.last_notification_error = None
        return final

    # No posts succeeded. If every trigger landed on a missing channel,
    # SKIP — re-running won't fix configuration. Otherwise it's transient.
    async with get_session() as s, s.begin():
        row = await s.get(VehicleOffer, lot_id)
        if row is None:
            log.warning(
                "lot vanished before transient/failed write", lot_id=lot_id,
            )
            return "transient"
        row.last_notification_error = last_error
        if not any_post_attempted:
            # Every trigger had no channel configured — ops misconfiguration,
            # re-trying won't help. Mark SKIPPED with the error recorded but
            # leave notification_attempts untouched (config errors aren't
            # delivery failures and must not consume the retry budget).
            row.notification_status = NotificationStatus.SKIPPED
            return "skipped"
        row.notification_attempts = (row.notification_attempts or 0) + 1
        if row.notification_attempts >= settings.notification_max_attempts:
            row.notification_status = NotificationStatus.FAILED
            log.error(
                "notification max attempts exceeded",
                lot_id=lot_id,
                attempts=row.notification_attempts,
                last_error=last_error,
            )
            return "failed"
        row.notification_status = NotificationStatus.PENDING
    return "transient"


async def process_pending(*, http_session: aiohttp.ClientSession) -> int:
    """Claim a batch of pending lots, process each in its own transaction.

    Returns the count of lots claimed (not successes — skips count too).

    Sequential by design — Discord rate limits apply per-bot globally across
    all channels; concurrent posts would race the rate limit instantly.

    Phase 13: when at least one lot finishes with outcome="transient" (i.e.
    a Discord blip left it PENDING for retry), self-NOTIFY notification_pending
    so the listener loop drains them next pass instead of waiting for a
    worker restart.
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
    outcomes: list[str] = []
    for lot_id in lot_ids:
        try:
            outcomes.append(
                await _process_one(lot_id, http_session=http_session),
            )
        except Exception:
            log.exception("process_one unhandled", lot_id=lot_id)
            outcomes.append("transient")
    if any(o == "transient" for o in outcomes):
        async with get_session() as s, s.begin():
            await notify(s, "notification_pending", "")
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
    # No new needs_plugin rows are produced today (the only producer was the
    # FAG router strategy, which was removed). This handler exists to drain
    # any historical NULL-`needs_plugin_notified_at` rows on retry.
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
        # Post-resolution invariant — see _process_one for the rationale.
        assert isinstance(channel_id, int), f"unresolved channel {channel_key!r}"
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
    lock_conn = await acquire_singleton_lock("notifier")
    try:
        # Resolve any string channel names → IDs once at startup so the
        # rest of the pipeline only deals with stable integer IDs. cast()
        # widens dict[str,int] to the field's declared dict[str,int|str];
        # dict values are invariant so the assignment can't be inferred.
        settings.discord_channels = cast(
            "dict[str, int | str]",
            await resolve_channels(
                settings.discord_channels,
                guild_id=settings.discord_guild_id,
                bot_token=settings.discord_bot_token,
            ),
        )
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
    finally:
        await lock_conn.close()
