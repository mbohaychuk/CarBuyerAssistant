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

from collections.abc import Awaitable, Callable

import structlog

from carbuyer.db.notify import notify
from carbuyer.db.session import get_session
from carbuyer.db.upserts import upsert_auction, upsert_lot_with_status_cascade
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock
from carbuyer.sources.base import SOURCES
from carbuyer.sources.hibid.source import HibidSource
from carbuyer.sources.mcdougall.source import McDougallSource

log = get_logger("ingester")

# A strategy is an async function that returns lots-ingested count.
# Registered in STRATEGIES below; dispatched once each per ingester run, with
# per-strategy try/except so one source failing doesn't abort siblings.
Strategy = Callable[[], Awaitable[int]]

# Bumped any time the cross-auction LotSearch query shape or parsing
# semantics change. Surfaces in AuctionLot.parser_version so stale rows
# get re-pending'd via the content-cascade in upsert_lot_with_status_cascade.
_HIBID_PARSER_VERSION = "hibid/v2.0-cross-auction"
_MCDOUGALL_PARSER_VERSION = "mcdougall/v2.0-listing-details"


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
    with structlog.contextvars.bound_contextvars(source=HibidSource.name):
        total = 0
        async with hibid_source:
            for province in settings.hibid_provinces:
                log.info("ingest province start", province=province)
                n = await _ingest_one_hibid_province(hibid_source, province)
                log.info("ingest province done", province=province, lots=n)
                total += n
        log.info("ingest complete", total_lots=total)
        return total


async def _run_mcdougall_lot_first() -> int:
    """Strategy: McDougall cross-auction Vehicles catalog lot-first ingestion.

    Walks products.php?category=Vehicles (one page at a time, paginated) and
    for each lot fetches its products-full-view.php?arg=<GUID> detail page.
    Each yielded (RawAuction, RawLot) pair is upserted in its own session.
    """
    mcdougall = SOURCES.get(McDougallSource.name)
    if not isinstance(mcdougall, McDougallSource):
        raise RuntimeError("mcdougall plugin did not self-register")
    with structlog.contextvars.bound_contextvars(source=McDougallSource.name):
        count = 0
        async with mcdougall:
            async for raw_auction, raw_lot in mcdougall.discover_vehicle_lots():
                async with get_session() as session, session.begin():
                    auction = await upsert_auction(
                        session, raw_auction, discovered_via="ingester",
                    )
                    lot = await upsert_lot_with_status_cascade(
                        session, auction.id, raw_lot,
                        parser_version=_MCDOUGALL_PARSER_VERSION,
                    )
                    await notify(session, "enrichment_pending", str(lot.id))
                    count += 1
        return count


# Strategy registration. Each entry's name appears in structured logs so a
# dropped or hanging source is easy to spot in journalctl. Order matters
# only for log readability; strategies are independent.
#
# Long-tail auctioneer discovery (smaller sites we don't yet plug) is
# handled out-of-band by a manual operator discovery workflow,
# which walks aggregator sites + emits a markdown report for human review.
# See docs/specs/2026-05-16-multi-source-ingestion.md Appendix A for the
# history of why automated long-tail ingestion was rejected.
STRATEGIES: list[tuple[str, Strategy]] = [
    ("hibid_lot_first", _run_hibid_lot_first),
    ("mcdougall_lot_first", _run_mcdougall_lot_first),
]


async def _dispatch_strategies() -> dict[str, int | None]:
    """Run each registered strategy under its own try/except.

    Returns a name -> lots-ingested mapping; None means the strategy raised
    (already logged at error level). The mapping is for the final summary
    log so the operator sees per-source counts in one line.

    Binds ``strategy=<name>`` as a structlog contextvar around each strategy
    invocation so per-strategy log lines are filterable in journalctl. The
    strategy itself binds ``source=<plugin-name>`` so logs inside source
    plugins also carry that attribution.
    """
    results: dict[str, int | None] = {}
    for name, strategy in STRATEGIES:
        with structlog.contextvars.bound_contextvars(strategy=name):
            log.info("ingest strategy start")
            try:
                count = await strategy()
            except Exception:
                log.exception("ingest strategy failed")
                results[name] = None
                continue
            log.info("ingest strategy done", lots=count)
            results[name] = count
    return results


async def main() -> None:
    """Entry point: acquire lock, dispatch all registered strategies, exit."""
    lock_conn = await acquire_singleton_lock("ingester")
    try:
        results = await _dispatch_strategies()
        log.info("ingest run done", **{f"strategy_{n}": c for n, c in results.items()})
    finally:
        await lock_conn.close()
