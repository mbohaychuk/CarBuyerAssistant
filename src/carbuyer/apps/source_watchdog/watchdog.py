"""Stale-source watchdog.

Runs hourly as a systemd timer. For each registered AuctionSource plugin,
computes ``now - MAX(auctions.last_seen_at)`` and posts a Discord alert to
the system_health channel if the gap exceeds STALE_THRESHOLD. Rate-limited
via the source_alert_state table to one alert per ALERT_DEDUP_WINDOW per
source — otherwise an hourly timer would generate 24 alerts/day per stale
source and train the operator to mute the channel.

Sources that have never ingested anything (no row in `auctions`) are NOT
alerted on. A brand-new plugin is the only case that hits this and the
operator already knows it's new; better to wait for first ingest than
flood at boot.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiohttp
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.notifier.channel_resolver import resolve_channels
from carbuyer.apps.notifier.discord_post import post_simple_message
from carbuyer.db.models import Auction, SourceAlertState
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock
from carbuyer.sources.base import SOURCES
from carbuyer.sources.hibid.source import HibidSource as _HibidSource  # noqa: F401
from carbuyer.sources.mcdougall.source import McDougallSource as _McDougallSource  # noqa: F401

log = get_logger("source_watchdog")

# A source that hasn't ingested in this long is considered stale. The
# ingester runs every 6h; allowing 4 missed cycles before alerting absorbs
# transient upstream outages without flapping.
STALE_THRESHOLD = timedelta(hours=24)

# Don't re-alert within this window. Slightly below STALE_THRESHOLD so a
# source that gets fixed-then-stale-again right at the boundary doesn't
# silently miss the second alert.
ALERT_DEDUP_WINDOW = timedelta(hours=23)


async def _max_last_seen_per_source(
    session: AsyncSession,
) -> dict[str, datetime]:
    """Aggregate auctions.last_seen_at by source. None-valued aggregates
    (source has never produced an auction) are omitted from the result —
    we don't alert on never-ingested sources."""
    stmt = select(Auction.source, func.max(Auction.last_seen_at)).group_by(
        Auction.source,
    )
    rows = (await session.execute(stmt)).all()
    return {row[0]: row[1] for row in rows if row[1] is not None}


async def _last_alerted_per_source(
    session: AsyncSession,
) -> dict[str, datetime]:
    rows = (
        await session.execute(
            select(SourceAlertState.source, SourceAlertState.last_alerted_at),
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _record_alert(
    session: AsyncSession, source: str, alerted_at: datetime,
) -> None:
    """Upsert source_alert_state row so dedup window starts now."""
    stmt = pg_insert(SourceAlertState).values(
        source=source, last_alerted_at=alerted_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source"],
        set_={"last_alerted_at": stmt.excluded.last_alerted_at},
    )
    await session.execute(stmt)


def _format_alert(source: str, last_seen: datetime, now: datetime) -> str:
    """Operator-facing one-line Discord message.

    Optimized for a 3am phone-glance: source name + age + actionable next
    step in one line. ISO timestamp omitted because the journal already
    has it under structured logging (search by source=).
    """
    gap = now - last_seen
    hours = int(gap.total_seconds() // 3600)
    return (
        f":warning: source `{source}` last ingested {hours}h ago. "
        f"Check `journalctl -u carbuyer-ingester.service`."
    )


async def _resolve_system_health_channel() -> int:
    """Resolve the configured system_health channel to a numeric ID.

    Raises ``RuntimeError`` on any unresolvable state. The watchdog's entire
    job is to alert when sources go dark; a silently-misconfigured watchdog
    would reintroduce the exact failure mode this app exists to close.
    Raising here exits the worker non-zero so systemd marks the timer as
    failed (``systemctl --failed`` / ``journalctl -u … --failed`` surface
    it) and the operator sees the misconfiguration loudly instead of
    discovering it the next time a real source goes silent unnoticed.
    """
    raw = settings.discord_channels.get("system_health")
    if raw is None:
        log.error(
            "system_health channel not configured; watchdog cannot alert. "
            "Add 'system_health' to DISCORD_CHANNELS.",
        )
        raise RuntimeError("system_health channel not configured")
    if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
        return int(raw)
    if not settings.discord_bot_token or settings.discord_guild_id is None:
        log.error(
            "system_health channel name configured but DISCORD_BOT_TOKEN / "
            "DISCORD_GUILD_ID missing; cannot resolve name to id",
        )
        raise RuntimeError(
            "cannot resolve channel name without bot_token + guild_id",
        )
    resolved = await resolve_channels(
        {"system_health": raw},
        guild_id=settings.discord_guild_id,
        bot_token=settings.discord_bot_token,
    )
    channel_id = resolved.get("system_health")
    if channel_id is None:
        log.error(
            "system_health channel name not found in guild",
            channel_name=raw,
        )
        raise RuntimeError(
            f"system_health channel {raw!r} not found in guild",
        )
    return channel_id


async def _check_and_alert(
    *, http_session: aiohttp.ClientSession, channel_id: int,
) -> int:
    """Single sweep across registered sources. Returns # alerts posted."""
    now = datetime.now(UTC)
    alerts_posted = 0
    async with get_session() as session, session.begin():
        last_seen_per_source = await _max_last_seen_per_source(session)
        last_alerted_per_source = await _last_alerted_per_source(session)

        for source_name in SOURCES:
            last_seen = last_seen_per_source.get(source_name)
            if last_seen is None:
                # Never ingested; the operator knows a fresh plugin won't
                # have data yet, no value in alerting.
                continue
            if now - last_seen < STALE_THRESHOLD:
                continue
            last_alerted = last_alerted_per_source.get(source_name)
            if (
                last_alerted is not None
                and now - last_alerted < ALERT_DEDUP_WINDOW
            ):
                log.info(
                    "stale source within dedup window; skipping",
                    source=source_name,
                    last_seen_at=last_seen.isoformat(),
                    last_alerted_at=last_alerted.isoformat(),
                )
                continue

            content = _format_alert(source_name, last_seen, now)
            ok = await post_simple_message(
                channel_id, content, session=http_session,
            )
            if not ok:
                # post_simple_message logs the HTTP-level failure; this
                # log line correlates the stale source with the delivery
                # outcome so a single journal grep on source= shows both.
                # Don't record the alert — next run retries.
                log.warning(
                    "stale source alert delivery failed; will retry next run",
                    source=source_name,
                )
                continue
            await _record_alert(session, source_name, now)
            alerts_posted += 1
            log.info(
                "stale source alert posted",
                source=source_name,
                last_seen_at=last_seen.isoformat(),
                stale_hours=int((now - last_seen).total_seconds() // 3600),
            )
    return alerts_posted


async def main() -> None:
    """One-shot watchdog run.

    Acquires the singleton lock to prevent concurrent invocations from racing
    on the source_alert_state upsert. Designed to fail loudly on any
    unrecoverable state (missing channel config, DB unreachable) — systemd
    Type=oneshot records the non-zero exit and the operator catches it via
    `systemctl --failed` or a journal alert. Transient blips recover on the
    next hourly tick.
    """
    lock_conn = await acquire_singleton_lock("source_watchdog")
    try:
        channel_id = await _resolve_system_health_channel()
        async with aiohttp.ClientSession() as http_session:
            posted = await _check_and_alert(
                http_session=http_session, channel_id=channel_id,
            )
        log.info("watchdog run complete", alerts_posted=posted)
    finally:
        await lock_conn.close()
