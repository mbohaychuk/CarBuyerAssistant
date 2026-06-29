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

from collections.abc import Awaitable, Callable, Sequence

import structlog
from pydantic import ValidationError
from sqlalchemy import func, update

from carbuyer.db.enums import EnrichmentStatus, ListingStatus, ValuationStatus
from carbuyer.db.models import PrivateListing
from carbuyer.db.notify import notify
from carbuyer.db.session import get_session
from carbuyer.db.upserts import (
    upsert_auction,
    upsert_lot_with_status_cascade,
    upsert_private_listing,
)
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock
from carbuyer.sources.base import SOURCES, ListingSource, RawLot
from carbuyer.sources.hibid.source import HibidSource
from carbuyer.sources.mcdougall.source import McDougallSource
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria
from carbuyer.wants.matcher import could_match_any_want

log = get_logger("ingester")


async def _load_active_criteria() -> list[WantCriteria]:
    """Validated criteria of every enabled want (one bad row skipped, not fatal).
    The want-first gate: no active wants → nothing is wanted."""
    async with get_session() as s:
        wants = await repo.list_wants(s, enabled_only=True)
    out: list[WantCriteria] = []
    for want in wants:
        try:
            out.append(WantCriteria.model_validate(want.config))
        except ValidationError:
            log.warning("skipping want with invalid config", want_id=want.id)
    return out


def _lot_wanted(raw_lot: RawLot, criteria_list: Sequence[WantCriteria]) -> bool:
    """WG2 gate: keep an auction lot only if it could match some active want, using
    scraped fields (parsed make/model/year or the title) — no LLM, no DB write yet."""
    return could_match_any_want(
        make=raw_lot.make, model=raw_lot.model, year=raw_lot.year,
        title=raw_lot.title, criteria_list=criteria_list,
    )

# A strategy is an async function that returns lots-ingested count.
# Registered in STRATEGIES below; dispatched once each per ingester run, with
# per-strategy try/except so one source failing doesn't abort siblings.
Strategy = Callable[[], Awaitable[int]]

# Bumped any time the cross-auction LotSearch query shape or parsing
# semantics change. Surfaces in AuctionLot.parser_version so stale rows
# get re-pending'd via the content-cascade in upsert_lot_with_status_cascade.
_HIBID_PARSER_VERSION = "hibid/v2.0-cross-auction"
_MCDOUGALL_PARSER_VERSION = "mcdougall/v1.0-catalog-walker"


async def _ingest_one_hibid_province(
    source: HibidSource, province: str, criteria_list: Sequence[WantCriteria],
) -> int:
    """Walk one province's HiBid lot stream end-to-end; return lots ingested. Lots
    matching no active want are dropped before any DB write (WG2)."""
    count = 0
    async for raw_auction, raw_lot in source.discover_vehicle_lots(province):
        if not _lot_wanted(raw_lot, criteria_list):
            continue
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
    criteria_list = await _load_active_criteria()
    if not criteria_list:  # want-first: no active wants → don't even scrape
        log.info("no active wants; skipping auction ingest", source=HibidSource.name)
        return 0
    with structlog.contextvars.bound_contextvars(source=HibidSource.name):
        total = 0
        async with hibid_source:
            for province in settings.hibid_provinces:
                log.info("ingest province start", province=province)
                n = await _ingest_one_hibid_province(hibid_source, province, criteria_list)
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
    criteria_list = await _load_active_criteria()
    if not criteria_list:  # want-first: no active wants → don't even scrape
        log.info("no active wants; skipping auction ingest", source=McDougallSource.name)
        return 0
    with structlog.contextvars.bound_contextvars(source=McDougallSource.name):
        count = 0
        async with mcdougall:
            async for raw_auction, raw_lot in mcdougall.discover_vehicle_lots():
                if not _lot_wanted(raw_lot, criteria_list):
                    continue
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


async def _pull_source(
    source: ListingSource,
    criterias: Sequence[WantCriteria],
) -> set[str]:
    """Pull one listing source across all want criteria; upsert + wake each
    listing; return the set of source_listing_ids seen this run.

    Want-list PULL: a source is asked for what matches each want rather than
    crawled wholesale. One listing can satisfy several wants, so the seen-set
    dedups within the run (the upsert is idempotent regardless) — and doubles as
    the disappearance signal: any active listing from this source NOT in the set
    has dropped out of every want's results.
    """
    seen: set[str] = set()
    async with source:
        for criteria in criterias:
            async for raw in source.search_listings(criteria):
                if raw.ref.source_listing_id in seen:
                    continue
                seen.add(raw.ref.source_listing_id)
                async with get_session() as session, session.begin():
                    listing = await upsert_private_listing(
                        session, raw, parser_version=source.version,
                    )
                    # Route the wake-up: a fresh/content-changed listing
                    # re-enriches (which then NOTIFYs valuation); a price-only
                    # change re-values directly.
                    if listing.enrichment_status == EnrichmentStatus.PENDING:
                        await notify(session, "enrichment_pending", str(listing.id))
                    elif listing.valuation_status == ValuationStatus.PENDING:
                        await notify(session, "valuation_pending", str(listing.id))
    return seen


async def _reconcile_disappeared(source: str, seen_ids: set[str]) -> int:
    """Mark active listings from ``source`` that this run did NOT return as
    removed (a sold/delisted car drops out of search). Stamps disappeared_at so
    the last-seen asking becomes a private-channel comp (scoring.comps).

    Skipped when the run saw nothing — an empty result is far more likely a
    transient glitch than every listing vanishing at once, and the caller only
    reconciles after a SUCCESSFUL pull so a source error can't mass-remove.
    """
    if not seen_ids:
        return 0
    # All columns are on the private_listing child table, so this is a plain
    # single-table UPDATE (no joined-inheritance multi-table write).
    async with get_session() as session, session.begin():
        result = await session.execute(
            update(PrivateListing)
            .where(
                PrivateListing.source == source,
                PrivateListing.listing_status == ListingStatus.ACTIVE.value,
                PrivateListing.source_listing_id.not_in(seen_ids),
            )
            .values(
                listing_status=ListingStatus.REMOVED.value,
                disappeared_at=func.now(),
            )
        )
    return int(getattr(result, "rowcount", 0) or 0)


async def _run_listing_pull() -> int:
    """Strategy: want-list PULL ingestion + disappearance reconciliation.

    No-op when there are no listing sources or no enabled wants. Each source is
    independent: a source whose pull raises is logged and skipped (and NOT
    reconciled, so a transient failure can't mass-remove its listings).
    """
    criterias = await _load_active_criteria()
    sources = [src for src in SOURCES.values() if isinstance(src, ListingSource)]
    if not sources or not criterias:
        return 0
    total = 0
    for source in sources:
        try:
            seen = await _pull_source(source, criterias)
        except Exception:
            log.exception("listing pull failed; skipping reconcile", source=source.name)
            continue
        total += len(seen)
        removed = await _reconcile_disappeared(source.name, seen)
        if removed:
            log.info("marked disappeared listings", source=source.name, count=removed)
    return total


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
    ("listing_pull", _run_listing_pull),
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
