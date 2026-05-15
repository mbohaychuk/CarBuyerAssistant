"""Lot-first ingester worker.

On invocation, walks `HibidSource.discover_vehicle_lots(province)` for
each province in `settings.hibid_provinces` and writes the resulting
auctions + lots to Postgres. Each lot is left at `enrichment_status =
PENDING` and a `NOTIFY enrichment_pending` is fired so the enricher
picks it up immediately.

Operationally a one-shot worker (run from a systemd timer, e.g. every
6h). Acquires the `ingester` advisory lock for the duration so two
concurrent invocations don't write conflicting upserts.
"""
from __future__ import annotations

from contextlib import AsyncExitStack

from carbuyer.apps.auction_discoverer.discoverer import upsert_auction
from carbuyer.apps.lot_scraper.scraper import upsert_lot_with_status_cascade
from carbuyer.db.notify import notify
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock
from carbuyer.sources.base import SOURCES
from carbuyer.sources.hibid.source import HibidSource

log = get_logger("ingester")

# Bumped any time the cross-auction LotSearch query shape or parsing
# semantics change. Surfaces in lot_scraper's parser_version field so
# stale rows get re-pending'd via the content-cascade.
_PARSER_VERSION = "hibid/v2.0-cross-auction"


async def _ingest_one_province(source: HibidSource, province: str) -> int:
    """Walk one province's lot stream end-to-end; return lots ingested."""
    count = 0
    async for raw_auction, raw_lot in source.discover_vehicle_lots(province):
        async with get_session() as session, session.begin():
            auction = await upsert_auction(
                session, raw_auction, discovered_via="ingester",
            )
            lot = await upsert_lot_with_status_cascade(
                session, auction.id, raw_lot, parser_version=_PARSER_VERSION,
            )
            await notify(session, "enrichment_pending", str(lot.id))
            count += 1
    return count


async def main() -> None:
    """Entry point: acquire lock, walk every configured province, exit."""
    lock_conn = await acquire_singleton_lock("ingester")
    try:
        async with AsyncExitStack() as stack:
            # HibidSource self-registered via register() at module import; the
            # plugin singleton owns its httpx client lifetime via async-cm.
            hibid_source = SOURCES.get(HibidSource.name)
            if not isinstance(hibid_source, HibidSource):
                raise RuntimeError("hibid plugin did not self-register")
            await stack.enter_async_context(hibid_source)
            total = 0
            for province in settings.hibid_provinces:
                log.info("ingest province start", province=province)
                n = await _ingest_one_province(hibid_source, province)
                log.info("ingest province done", province=province, lots=n)
                total += n
            log.info("ingest complete", total_lots=total)
    finally:
        await lock_conn.close()
