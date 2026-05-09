from __future__ import annotations

from contextlib import AsyncExitStack

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import listen, notify
from carbuyer.db.session import get_session
from carbuyer.shared.logging import get_logger
from carbuyer.sources.base import (
    SOURCES,
    AuctionFetcher,
    AuctionRef,
    RawLot,
)
from carbuyer.sources.hibid.source import HibidSource as _HibidSource  # registers plugin

_REGISTERED_PLUGINS = (_HibidSource.name,)

log = get_logger("lot_scraper")


# Fields that, when changed, invalidate downstream worker output.
# Bid columns are deliberately NOT here — they're the bid-poller's domain.
_CONTENT_TRIGGER_FIELDS: tuple[str, ...] = (
    "title", "description", "photos",
    "year", "make", "model", "vin", "mileage_km",
)


async def _upsert_lot(
    session: AsyncSession,
    auction_id: int,
    raw: RawLot,
    *,
    parser_version: str,
) -> AuctionLot:
    """Atomic UPSERT on (auction_id, source_lot_id) with coalesce-on-update.

    Caller wraps in upsert_lot_with_status_cascade to apply the trigger-field
    cascade. This inner function is the pure SQL operation.
    """
    insert_values: dict[str, object] = {
        "auction_id": auction_id,
        "source_lot_id": raw.ref.source_lot_id,
        "lot_number": raw.lot_number,
        "url": raw.ref.url,
        "parser_version": parser_version,
        "title": raw.title,
        "description": raw.description,
        "photos": raw.photos,
        "year": raw.year,
        "make": raw.make,
        "model": raw.model,
        "trim": raw.trim,
        "mileage_km": raw.mileage_km,
        "vin": raw.vin,
        "lot_status": raw.lot_status,
    }
    stmt = pg_insert(AuctionLot).values(**insert_values)
    excluded = stmt.excluded

    # Per Phase 0 column-ownership: lot-scraper does NOT write bid columns.
    # On conflict, the only mutations are content (with coalesce) + parser_version.
    update_values: dict[str, object] = {
        "url": excluded.url,
        "lot_number": func.coalesce(excluded.lot_number, AuctionLot.lot_number),
        "parser_version": excluded.parser_version,
        "lot_status": excluded.lot_status,
    }
    for field_name in (*_CONTENT_TRIGGER_FIELDS, "trim"):
        update_values[field_name] = func.coalesce(
            getattr(excluded, field_name), getattr(AuctionLot, field_name),
        )
    stmt = stmt.on_conflict_do_update(
        index_elements=["auction_id", "source_lot_id"],
        set_=update_values,
    ).returning(AuctionLot)
    result = await session.execute(
        stmt, execution_options={"populate_existing": True},
    )
    return result.scalar_one()


async def upsert_lot_with_status_cascade(
    session: AsyncSession,
    auction_id: int,
    raw: RawLot,
    *,
    parser_version: str,
) -> AuctionLot:
    """UPSERT a lot; reset all four pipeline statuses if any content trigger
    field or parser_version changed. Idempotent re-scrapes preserve status.
    """
    pre = (
        await session.execute(
            select(AuctionLot).where(
                AuctionLot.auction_id == auction_id,
                AuctionLot.source_lot_id == raw.ref.source_lot_id,
            ),
        )
    ).scalar_one_or_none()
    if pre is not None:
        pre_snapshot: dict[str, object] | None = {
            f: getattr(pre, f) for f in _CONTENT_TRIGGER_FIELDS
        }
        pre_parser_version: str | None = pre.parser_version
    else:
        pre_snapshot = None
        pre_parser_version = None

    lot = await _upsert_lot(session, auction_id, raw, parser_version=parser_version)

    if pre_snapshot is None:
        # Fresh insert — server defaults already set all statuses to PENDING.
        return lot

    post_snapshot = {f: getattr(lot, f) for f in _CONTENT_TRIGGER_FIELDS}
    content_changed = post_snapshot != pre_snapshot
    parser_changed = lot.parser_version != pre_parser_version
    if content_changed or parser_changed:
        lot.enrichment_status = EnrichmentStatus.PENDING
        lot.valuation_status = ValuationStatus.PENDING
        lot.vision_status = VisionStatus.PENDING
        lot.notification_status = NotificationStatus.PENDING
        await session.flush()
    return lot


async def process_auction(
    auction_id: int,
    fetchers: dict[str, AuctionFetcher],
) -> int:
    """Scrape every lot for one auction. Per-lot transaction; HTTP I/O outside
    the txn so connection pools and idle_in_transaction timeouts don't suffer.
    """
    async with get_session() as s:
        auction = await s.get(Auction, auction_id)
    if auction is None:
        log.warning("auction not found", auction_id=auction_id)
        return 0
    if auction.source.startswith("unknown:"):
        log.info(
            "skipping unknown-platform auction (no fetcher plugin)",
            auction_id=auction_id, source=auction.source,
        )
        return 0
    fetcher = fetchers.get(auction.source)
    if fetcher is None:
        log.warning(
            "no fetcher plugin registered",
            auction_id=auction_id, source=auction.source,
        )
        return 0
    aref = AuctionRef(
        source=auction.source,
        source_auction_id=auction.source_auction_id,
        url=auction.url,
    )
    count = 0
    async for lot_ref in fetcher.fetch_lots(aref):
        try:
            raw = await fetcher.fetch_lot(lot_ref)
        except Exception:
            log.exception("fetch_lot failed", lot_ref_url=lot_ref.url)
            continue
        async with get_session() as session, session.begin():
            lot = await upsert_lot_with_status_cascade(
                session, auction.id, raw,
                parser_version=fetcher.version,
            )
            await notify(session, "enrichment_pending", str(lot.id))
        count += 1
    return count


async def _catchup_sweep(fetchers: dict[str, AuctionFetcher]) -> None:
    """At startup + reconnect, find auctions whose lots haven't been scraped
    yet (i.e. nothing in auction_lots for them) and process them. NOTIFYs
    fired while the worker was down land here.
    """
    async with get_session() as s:
        result = await s.execute(
            select(Auction.id).where(
                ~select(AuctionLot.id)
                .where(AuctionLot.auction_id == Auction.id)
                .exists(),
                ~Auction.source.startswith("unknown:"),
            ),
        )
        ids = list(result.scalars().all())
    for auction_id in ids:
        log.info("catchup processing", auction_id=auction_id)
        try:
            await process_auction(auction_id, fetchers)
        except Exception:
            log.exception("catchup process_auction failed", auction_id=auction_id)


async def main() -> None:
    for name in _REGISTERED_PLUGINS:
        if name not in SOURCES:
            raise RuntimeError(f"plugin {name!r} failed to self-register at import")
    fetchers: dict[str, AuctionFetcher] = {
        s.name: s for s in SOURCES.values() if isinstance(s, AuctionFetcher)
    }
    async with AsyncExitStack() as stack:
        for f in fetchers.values():
            await stack.enter_async_context(f)
        await _catchup_sweep(fetchers)
        async for payload in listen("auction_pending"):
            try:
                auction_id = int(payload)
            except ValueError:
                continue
            log.info("processing", auction_id=auction_id)
            try:
                await process_auction(auction_id, fetchers)
            except Exception:
                log.exception("process_auction failed", auction_id=auction_id)
