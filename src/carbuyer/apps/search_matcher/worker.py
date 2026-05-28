"""Saved-search matcher worker.

Single-instance LISTEN/NOTIFY worker. On a per-lot NOTIFY (valuation_pending or
notification_pending carrying str(lot.id)) it matches that lot against all
active searches. These channels are also emitted with an empty payload as
broadcast wakes (valuator self-notify, /admin/rescore); those are ignored —
per-lot re-notifies plus the startup backfill cover them (see plan Decision 1).
On saved_search_changed (carries a search_id) it backfills that one search
against all active lots. At startup it backfills the full cross product to
recover NOTIFYs missed while down. Inserts are idempotent
(ON CONFLICT DO NOTHING), so re-running any handler only ever adds first-seen
matches; matches are never retracted (spec §2.2)."""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import LotStatus
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch
from carbuyer.db.notify import listen
from carbuyer.db.saved_searches import MatchableListing, adapt_auction_lot, match_listing
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock

log = get_logger("search_matcher")

# A lot is matchable while it is still biddable. Mirrors dashboard
# OPEN_STATUSES but defined here to avoid importing the dashboard from a worker.
_ACTIVE_LOT_STATUSES: tuple[str, ...] = (
    LotStatus.OPEN.value,
    LotStatus.CLOSING_SOON.value,
    LotStatus.EXTENDED.value,
)

# Lot-data channels the matcher reacts to. valuation_pending fires once the
# enricher has populated vehicle fields; notification_pending fires once the
# valuator has populated all_in_at_current_bid_cad + rarity_score. See the PR-2
# plan "Decisions" section for why enrichment_pending is deliberately excluded.
_LOT_CHANNELS: tuple[str, ...] = ("valuation_pending", "notification_pending")
_SEARCH_CHANNEL = "saved_search_changed"


async def _active_searches(session: AsyncSession) -> list[SavedSearch]:
    stmt = select(SavedSearch).where(SavedSearch.is_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def _active_listings(session: AsyncSession, *, limit: int) -> list[MatchableListing]:
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.lot_status.in_(_ACTIVE_LOT_STATUSES))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [adapt_auction_lot(lot, auction) for lot, auction in rows]


async def _insert_matches(session: AsyncSession, triples: list[tuple[int, str, int]]) -> None:
    if not triples:
        return
    values = [
        {"saved_search_id": sid, "source_kind": kind, "source_id": sid_src}
        for sid, kind, sid_src in triples
    ]
    stmt = pg_insert(SavedSearchMatch).values(values).on_conflict_do_nothing(
        index_elements=["saved_search_id", "source_kind", "source_id"],
    )
    await session.execute(stmt)


async def process_lot(lot_id: int) -> int:
    """Match one lot against all active searches. Returns the number of
    (search, lot) pairs that matched (pre-dedup)."""
    async with get_session() as s, s.begin():
        pair = (await s.execute(
            select(AuctionLot, Auction)
            .join(Auction, Auction.id == AuctionLot.auction_id)
            .where(AuctionLot.id == lot_id)
        )).first()
        if pair is None:
            return 0
        lot, auction = pair
        listing = adapt_auction_lot(lot, auction)
        searches = await _active_searches(s)
        hits = [sch for sch in searches if match_listing(listing, sch)]
        await _insert_matches(
            s, [(sch.id, listing.source_kind, listing.source_id) for sch in hits],
        )
        return len(hits)


async def process_search(search_id: int) -> int:
    """Backfill one search against all active lots. Returns matched-lot count."""
    async with get_session() as s, s.begin():
        search = await s.get(SavedSearch, search_id)
        if search is None or not search.is_active:
            return 0
        listings = await _active_listings(s, limit=settings.search_match_backfill_limit)
        hits = [lst for lst in listings if match_listing(lst, search)]
        await _insert_matches(
            s, [(search.id, lst.source_kind, lst.source_id) for lst in hits],
        )
        return len(hits)


async def startup_backfill() -> int:
    """Match the full active-search x active-lot cross product. Catchup for
    NOTIFYs missed while the worker was down. Returns matched-pair count."""
    async with get_session() as s, s.begin():
        searches = await _active_searches(s)
        listings = await _active_listings(s, limit=settings.search_match_backfill_limit)
        triples: list[tuple[int, str, int]] = [
            (sch.id, lst.source_kind, lst.source_id)
            for sch in searches
            for lst in listings
            if match_listing(lst, sch)
        ]
        await _insert_matches(s, triples)
        return len(triples)


async def _lot_loop(channel: str) -> None:
    async for payload in listen(channel):
        if not payload:
            continue
        try:
            await process_lot(int(payload))
        except Exception:
            log.exception("lot match failed; sleeping", channel=channel, payload=payload)
            await asyncio.sleep(5)


async def _search_loop() -> None:
    async for payload in listen(_SEARCH_CHANNEL):
        if not payload:
            continue
        try:
            await process_search(int(payload))
        except Exception:
            log.exception("search backfill failed; sleeping", payload=payload)
            await asyncio.sleep(5)


async def main() -> None:
    lock_conn = await acquire_singleton_lock("search_matcher")
    try:
        matched = await startup_backfill()
        log.info("startup backfill complete", matched_pairs=matched)
        async with asyncio.TaskGroup() as tg:
            for ch in _LOT_CHANNELS:
                tg.create_task(_lot_loop(ch), name=f"lot_loop:{ch}")
            tg.create_task(_search_loop(), name="search_loop")
    finally:
        await lock_conn.close()
