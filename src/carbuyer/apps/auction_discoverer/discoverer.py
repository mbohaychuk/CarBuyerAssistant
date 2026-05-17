from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Auction
from carbuyer.db.notify import notify
from carbuyer.db.session import get_session
from carbuyer.shared.logging import get_logger
from carbuyer.sources.base import (
    SOURCES,
    AuctionDiscoverer,
    AuctionFetcher,
    AuctionRef,
    RawAuction,
)
from carbuyer.sources.farmauctionguide.source import (
    FarmAuctionGuideSource as _FagSource,  # registers plugin
)
from carbuyer.sources.hibid.source import HibidSource as _HibidSource  # registers plugin
from carbuyer.sources.mcdougall.source import (
    McDougallSource as _McDougallSource,  # registers plugin
)
from carbuyer.sources.resolver import canonicalize_url

_REGISTERED_PLUGINS = (_HibidSource.name, _McDougallSource.name, _FagSource.name)

log = get_logger("auction_discoverer")


def minimal_raw_auction(ref: AuctionRef) -> RawAuction:
    """For routed refs without a fetcher plugin -- records bare auction
    metadata so the row exists for /needs-plugin triage. Also used by the
    ingester's FAG router strategy where lots come from per-platform
    strategies and the FAG sweep contributes auction metadata only.
    """
    return RawAuction(
        ref=ref,
        title=None, description=None,
        auctioneer_name=None, auctioneer_external_id=None,
        scheduled_start_at=None, scheduled_end_at=None,
        pickup_address=None, pickup_city=None, pickup_province=None,
        pickup_window_text=None,
        buyer_premium_pct=None, online_bidding_fee_pct=None,
        terms_text=None, auction_subtype="estate",
    )


async def upsert_auction(
    session: AsyncSession,
    raw: RawAuction,
    *,
    discovered_via: str,
) -> Auction:
    """Atomic UPSERT keyed on (source, source_auction_id).

    On conflict: refreshes last_seen_at, copies non-None fields from `raw` (never
    overwrites with None via ``coalesce(EXCLUDED, table)``), and appends
    `discovered_via` to the array if not already present.
    """
    now = datetime.now(UTC)
    canonical = canonicalize_url(raw.ref.url)

    insert_values: dict[str, object] = {
        "source": raw.ref.source,
        "source_auction_id": raw.ref.source_auction_id,
        "url": raw.ref.url,
        "canonical_url": canonical,
        "discovered_via": [discovered_via],
        "auction_subtype": raw.auction_subtype,
        "auctioneer_name": raw.auctioneer_name,
        "auctioneer_external_id": raw.auctioneer_external_id,
        "title": raw.title,
        "description": raw.description,
        "terms_text": raw.terms_text,
        "scheduled_start_at": raw.scheduled_start_at,
        "scheduled_end_at": raw.scheduled_end_at,
        "pickup_address": raw.pickup_address,
        "pickup_city": raw.pickup_city,
        "pickup_province": raw.pickup_province,
        "pickup_window_text": raw.pickup_window_text,
        "buyer_premium_pct": raw.buyer_premium_pct,
        "buyer_premium_max_cad": raw.buyer_premium_max_cad,
        "buyer_premium_min_cad": raw.buyer_premium_min_cad,
        "online_bidding_fee_pct": raw.online_bidding_fee_pct,
        "status": "upcoming",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    stmt = pg_insert(Auction).values(**insert_values)
    excluded = stmt.excluded
    update_set: dict[str, object] = {
        "url": excluded.url,
        "canonical_url": excluded.canonical_url,
        "auction_subtype": func.coalesce(
            excluded.auction_subtype, Auction.auction_subtype,
        ),
        "auctioneer_name": func.coalesce(
            excluded.auctioneer_name, Auction.auctioneer_name,
        ),
        "auctioneer_external_id": func.coalesce(
            excluded.auctioneer_external_id, Auction.auctioneer_external_id,
        ),
        "title": func.coalesce(excluded.title, Auction.title),
        "description": func.coalesce(excluded.description, Auction.description),
        "terms_text": func.coalesce(excluded.terms_text, Auction.terms_text),
        "scheduled_start_at": func.coalesce(
            excluded.scheduled_start_at, Auction.scheduled_start_at,
        ),
        "scheduled_end_at": func.coalesce(
            excluded.scheduled_end_at, Auction.scheduled_end_at,
        ),
        "pickup_address": func.coalesce(
            excluded.pickup_address, Auction.pickup_address,
        ),
        "pickup_city": func.coalesce(excluded.pickup_city, Auction.pickup_city),
        "pickup_province": func.coalesce(
            excluded.pickup_province, Auction.pickup_province,
        ),
        "pickup_window_text": func.coalesce(
            excluded.pickup_window_text, Auction.pickup_window_text,
        ),
        "buyer_premium_pct": func.coalesce(
            excluded.buyer_premium_pct, Auction.buyer_premium_pct,
        ),
        "buyer_premium_max_cad": func.coalesce(
            excluded.buyer_premium_max_cad, Auction.buyer_premium_max_cad,
        ),
        "buyer_premium_min_cad": func.coalesce(
            excluded.buyer_premium_min_cad, Auction.buyer_premium_min_cad,
        ),
        "online_bidding_fee_pct": func.coalesce(
            excluded.online_bidding_fee_pct, Auction.online_bidding_fee_pct,
        ),
        "last_seen_at": excluded.last_seen_at,
        "updated_at": func.now(),  # ORM onupdate doesn't fire on ON CONFLICT
        # Atomic dedup-append: array || EXCLUDED.array, then DISTINCT.
        "discovered_via": text(
            "ARRAY(SELECT DISTINCT unnest("
            "auctions.discovered_via || EXCLUDED.discovered_via))",
        ),
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "source_auction_id"],
        set_=update_set,
    ).returning(Auction)
    # populate_existing=True so RETURNING overrides any stale instance in the
    # session's identity map (otherwise an UPSERT update wouldn't refresh the
    # earlier-loaded ORM object's attributes).
    result = await session.execute(stmt, execution_options={"populate_existing": True})
    return result.scalar_one()


async def _sweep_one_discoverer(
    discoverer: AuctionDiscoverer,
    fetchers: dict[str, AuctionFetcher],
) -> int:
    found = 0
    log.info("discovering", source=discoverer.name)
    async for ref in discoverer.discover_auctions():
        if ref.source.startswith("unknown:"):
            log.warning(
                "unknown platform discovered",
                router=discoverer.name, source=ref.source, url=ref.url,
            )
            raw: RawAuction = minimal_raw_auction(ref)
        else:
            fetcher = fetchers.get(ref.source)
            if fetcher is None:
                log.warning(
                    "no fetcher for resolved source — recording metadata only",
                    router=discoverer.name, source=ref.source,
                )
                raw = minimal_raw_auction(ref)
            else:
                try:
                    raw = await fetcher.fetch_auction(ref)
                except Exception:
                    log.exception(
                        "fetch_auction failed",
                        source=ref.source, ref_url=ref.url,
                    )
                    continue
        async with get_session() as session, session.begin():
            auction = await upsert_auction(
                session, raw, discovered_via=discoverer.name,
            )
            await notify(session, "auction_pending", str(auction.id))
            if (
                ref.source.startswith("unknown:")
                and auction.needs_plugin_notified_at is None
            ):
                await notify(session, "needs_plugin", str(auction.id))
                log.info(
                    "needs_plugin notify emitted",
                    source=ref.source,
                    auction_id=auction.id,
                    discoverer=discoverer.name,
                )
        found += 1
    return found


async def discover_once() -> int:
    """One sweep across every registered discoverer. Returns # auctions surfaced."""
    discoverers = [s for s in SOURCES.values() if isinstance(s, AuctionDiscoverer)]
    fetchers: dict[str, AuctionFetcher] = {
        s.name: s for s in SOURCES.values() if isinstance(s, AuctionFetcher)
    }
    total = 0
    async with AsyncExitStack() as stack:
        # Enter every plugin's async-CM ONCE for the duration of the sweep.
        entered: set[int] = set()
        for d in discoverers:
            await stack.enter_async_context(d)
            entered.add(id(d))
        for f in fetchers.values():
            if id(f) not in entered:
                await stack.enter_async_context(f)
                entered.add(id(f))
        for d in discoverers:
            try:
                total += await _sweep_one_discoverer(d, fetchers)
            except Exception:
                log.exception("discoverer sweep failed", source=d.name)
                continue
    log.info("discovery complete", found=total)
    return total


async def main() -> None:
    # _REGISTERED_PLUGINS imports each platform module at module load, which
    # triggers register() into SOURCES. Add new platforms by importing them here.
    for name in _REGISTERED_PLUGINS:
        if name not in SOURCES:
            raise RuntimeError(f"plugin {name!r} failed to self-register at import")
    await discover_once()
