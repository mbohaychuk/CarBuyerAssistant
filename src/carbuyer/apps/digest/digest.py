"""Daily want-list digest — nightly cron worker.

Distiller-shaped: no LISTEN, no claim, single-instance, runs once and exits.
Delivers every want match still un-notified (digest-tier matches plus instant
matches deferred by quiet hours) as one grouped Discord message, then stamps
notified_at so each is delivered exactly once. Schedule at quiet_hours_end so a
quiet-deferred standout deal is the first thing delivered in the morning.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import aiohttp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import DigestRow, render_digest_text
from carbuyer.apps.notifier.channel_resolver import resolve_channels
from carbuyer.apps.notifier.discord_post import post_simple_message
from carbuyer.db.models import Search, VehicleOffer, WantMatch
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("digest")


def _vehicle_title(o: VehicleOffer) -> str:
    parts = [str(o.year or ""), o.make or "", o.model or "", o.trim or ""]
    return " ".join(p for p in parts if p).strip() or (o.title or f"Offer #{o.id}")


async def build_digest(
    session: AsyncSession,
) -> tuple[list[int], list[tuple[str, list[DigestRow]]]]:
    """Un-notified, non-dismissed matches for enabled wants, grouped by want name.
    Returns (match_ids, groups) — match_ids to stamp, groups to render."""
    rows = (
        await session.execute(
            select(WantMatch.id, WantMatch.want_relative_score, Search.name, VehicleOffer)
            .join(Search, Search.id == WantMatch.search_id)
            .join(VehicleOffer, VehicleOffer.id == WantMatch.lot_id)
            .where(
                WantMatch.notified_at.is_(None),
                WantMatch.dismissed.is_(False),
                Search.enabled.is_(True),
            )
            .order_by(Search.name, WantMatch.want_relative_score.desc().nulls_last())
        )
    ).all()
    match_ids: list[int] = []
    grouped: dict[str, list[DigestRow]] = {}
    for match_id, score, want_name, offer in rows:
        match_ids.append(match_id)
        grouped.setdefault(want_name, []).append(
            DigestRow(
                title=_vehicle_title(offer),
                price_cad=offer.offer_price,
                pct_below_market=score,
                url=offer.url,
            )
        )
    return match_ids, list(grouped.items())


async def main(now: datetime | None = None) -> None:
    if now is None:
        now = datetime.now(UTC)
    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN not configured")
        return
    settings.discord_channels = cast(
        "dict[str, int | str]",
        await resolve_channels(
            settings.discord_channels,
            guild_id=settings.discord_guild_id,
            bot_token=settings.discord_bot_token,
        ),
    )
    channel_key = select_channel(trigger="want_match", score=None)  # "wants"
    channel_id = settings.discord_channels.get(channel_key)
    if not isinstance(channel_id, int):
        log.warning("no wants channel configured for digest", channel_key=channel_key)
        return

    async with get_session() as session:
        match_ids, groups = await build_digest(session)
    if not match_ids:
        log.info("digest: nothing to deliver")
        return

    content = render_digest_text(groups)
    async with aiohttp.ClientSession() as http:
        posted = await post_simple_message(channel_id, content, session=http)
    if not posted:
        log.warning("digest post failed; leaving matches un-notified for next run")
        return

    async with get_session() as session, session.begin():
        await session.execute(
            update(WantMatch).where(WantMatch.id.in_(match_ids)).values(notified_at=now)
        )
    log.info("digest delivered", matches=len(match_ids), wants=len(groups))
