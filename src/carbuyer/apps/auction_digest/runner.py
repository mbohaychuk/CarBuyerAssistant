"""Per-auction digest cron runner: select eligible auctions, compose, post, mark.

Eligibility (spec PR-3 §3.1): scheduled_start_at set and within the next 24h,
not yet digested, status not cancelled/past. Each auction is processed in its
own short transaction; compose -> (if non-empty) post -> stamp digest_sent_at.
An empty composition still stamps digest_sent_at so it isn't re-evaluated for
24h. Single-instance (advisory lock in main) so overlapping timer fires can't
double-post."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiohttp
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from carbuyer.apps.auction_digest.composer import (
    DigestHeader,
    DigestLot,
    compose_digest,
)
from carbuyer.apps.notifier.channel_resolver import resolve_channels
from carbuyer.apps.notifier.discord_post import post_simple_message
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock

log = get_logger("auction_digest")

_DIGEST_KEY = "auction_digest"
_FALLBACK_KEY = "early_warning"  # spec §3.4: default to the existing alerts channel
_SKIP_STATUSES = ("cancelled", "past")
_WINDOW_HOURS = 24


def _lot_summary(lot: AuctionLot) -> str:
    # Use the lot title as the human-readable label (set by the scraper).
    # Append mileage when we have it for quick scan-ability.
    name = lot.title or f"Lot {lot.id}"
    if lot.mileage_km is not None:
        name += f" - {lot.mileage_km // 1000}k km"
    return name


async def _resolve_digest_channel() -> int | None:
    """Resolve the digest channel id (auction_digest, falling back to the
    existing alerts channel). Returns None if neither is configured."""
    resolved = await resolve_channels(
        settings.discord_channels,
        guild_id=settings.discord_guild_id,
        bot_token=settings.discord_bot_token,
    )
    return resolved.get(_DIGEST_KEY) or resolved.get(_FALLBACK_KEY)


async def _eligible_auction_ids(session: AsyncSession, *, now: datetime) -> list[int]:
    window_end = now + timedelta(hours=_WINDOW_HOURS)
    stmt = (
        select(Auction.id)
        .where(
            Auction.scheduled_start_at.is_not(None),
            Auction.scheduled_start_at > now,
            Auction.scheduled_start_at <= window_end,
            Auction.digest_sent_at.is_(None),
            Auction.status.notin_(_SKIP_STATUSES),
        )
        .order_by(Auction.scheduled_start_at)
    )
    return list((await session.execute(stmt)).scalars().all())


def _active_lot_clause() -> ColumnElement[bool]:
    # Include lots where user_action is NULL or not PASSED.
    return or_(
        AuctionLot.user_action.is_(None),
        AuctionLot.user_action != UserAction.PASSED.value,
    )


async def _build_sections(
    session: AsyncSession, auction: Auction,
) -> tuple[list[DigestLot], list[DigestLot]]:
    # Section 1: saved-search matches (dismissed + passed excluded), annotated.
    match_rows = (await session.execute(
        select(AuctionLot, SavedSearch.name)
        .join(SavedSearchMatch, (SavedSearchMatch.source_kind == "auction_lot")
              & (SavedSearchMatch.source_id == AuctionLot.id))
        .join(SavedSearch, SavedSearch.id == SavedSearchMatch.saved_search_id)
        .where(
            AuctionLot.auction_id == auction.id,
            SavedSearchMatch.dismissed_at.is_(None),
            _active_lot_clause(),
            AuctionLot.year.is_not(None),
        )
        .order_by(SavedSearchMatch.matched_at.desc(), SavedSearchMatch.id.desc())
    )).all()
    seen: set[int] = set()
    matches: list[DigestLot] = []
    for lot, search_name in match_rows:
        if lot.id in seen:
            continue
        seen.add(lot.id)
        matches.append(DigestLot(lot.id, _lot_summary(lot), search_name))

    # Section 2: rare/special, excluding lots already in section 1.
    rare_rows = (await session.execute(
        select(AuctionLot)
        .where(
            AuctionLot.auction_id == auction.id,
            AuctionLot.rarity_score.is_not(None),
            AuctionLot.rarity_score >= settings.digest_rarity_threshold,
            _active_lot_clause(),
            AuctionLot.year.is_not(None),
        )
        .order_by(AuctionLot.rarity_score.desc())
    )).scalars().all()
    rare = [DigestLot(lot.id, _lot_summary(lot), None) for lot in rare_rows if lot.id not in seen]
    return matches, rare


async def run_digests(
    *,
    now: datetime,
    http_session: aiohttp.ClientSession | None = None,
) -> dict[str, int]:
    """One cron pass. `now` is injected for deterministic tests."""
    async with get_session() as s:
        ids = await _eligible_auction_ids(s, now=now)
    counts: dict[str, int] = {"posted": 0, "empty": 0, "failed": 0}
    if not ids:
        log.info("auction_digest: no eligible auctions")
        return counts

    channel_id = await _resolve_digest_channel()
    if channel_id is None:
        log.error("auction_digest: no channel configured (auction_digest/early_warning)")
        return counts

    owns_session = http_session is None
    http = http_session or aiohttp.ClientSession()
    try:
        for auction_id in ids:
            try:
                async with get_session() as s, s.begin():
                    auction = await s.get(Auction, auction_id)
                    if auction is None or auction.digest_sent_at is not None:
                        continue
                    matches, rare = await _build_sections(s, auction)
                    total_lots = (await s.execute(
                        select(func.count()).select_from(AuctionLot).where(
                            AuctionLot.auction_id == auction_id,
                        )
                    )).scalar_one()
                    vehicle_count = (await s.execute(
                        select(func.count()).select_from(AuctionLot).where(
                            AuctionLot.auction_id == auction_id,
                            AuctionLot.year.is_not(None),
                        )
                    )).scalar_one()
                    header = DigestHeader(
                        auction_id=auction.id,
                        title=auction.title or auction.source,
                        location=", ".join(
                            p for p in [auction.pickup_city, auction.pickup_province] if p
                        ) or "?",
                        starts_at=auction.scheduled_start_at,
                        lot_count=total_lots,
                        vehicle_count=vehicle_count,
                        url=auction.canonical_url,
                    )
                    content = compose_digest(header, matches=matches, rare=rare)
                    if content is None:
                        auction.digest_sent_at = func.now()  # type: ignore[assignment]
                        counts["empty"] += 1
                        continue
                    ok = await post_simple_message(channel_id, content, session=http)
                    if ok:
                        auction.digest_sent_at = func.now()  # type: ignore[assignment]
                        counts["posted"] += 1
                    else:
                        counts["failed"] += 1
                        log.warning("auction_digest post failed", auction_id=auction_id)
            except Exception:
                log.exception("auction_digest: auction failed", auction_id=auction_id)
                counts["failed"] += 1
    finally:
        if owns_session:
            await http.close()
    log.info("auction_digest complete", **counts)
    return counts


async def main(now: datetime | None = None) -> None:
    lock_conn = await acquire_singleton_lock("auction_digest")
    try:
        if now is None:
            now = datetime.now(UTC)
        async with aiohttp.ClientSession() as http:
            await run_digests(now=now, http_session=http)
    finally:
        await lock_conn.close()
