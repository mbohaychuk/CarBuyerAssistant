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
_HIBID_PARSER_VERSION = "hibid/v2.0-cross-auction"


async def _ingest_one_hibid_province(source: HibidSource, province: str) -> int:
    """Walk one province's HiBid lot stream end-to-end; return lots ingested."""
    count = 0
    async for raw_auction, raw_lot in source.discover_vehicle_lots(province):
        async with get_session() as session, session.begin():
            auction = await upsert_auction(
                session, raw_auction, discovered_via="ingester",
            )
            lot = await upsert_lot_with_status_cascade(
                session, auction.id, raw_lot, parser_version=_HIBID_PARSER_VERSION,
            )
            await notify(session, "enrichment_pending", str(lot.id))
            count += 1
    return count


async def _run_hibid_lot_first() -> int:
    """Strategy: HiBid cross-auction GraphQL lot-first ingestion.

    HiBid exposes a cross-auction LotSearch operation that returns every
    vehicle lot in one round-trip per province -- much more efficient than the
    generic per-auction page walk other plugins use. Lives in its own strategy
    function so the multi-source dispatch loop can isolate failures per source.
    """
    hibid_source = SOURCES.get(HibidSource.name)
    if not isinstance(hibid_source, HibidSource):
        raise RuntimeError("hibid plugin did not self-register")
    total = 0
    async with hibid_source:
        for province in settings.hibid_provinces:
            log.info("ingest province start", province=province)
            n = await _ingest_one_hibid_province(hibid_source, province)
            log.info("ingest province done", province=province, lots=n)
            total += n
    log.info("ingest complete", total_lots=total)
    return total


async def main() -> None:
    """Entry point: acquire lock, run each ingestion strategy, exit."""
    lock_conn = await acquire_singleton_lock("ingester")
    try:
        await _run_hibid_lot_first()
    finally:
        await lock_conn.close()
