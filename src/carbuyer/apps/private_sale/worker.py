"""Private-sale cron worker: scrape → upsert → enrich → value → match → alert.

Architecture mirrors ``apps/auction_digest/runner.py``:
  - ``main(now=None)``: singleton lock + aiohttp session, runs once per cron fire.
  - ``run_cycle(now, *, source, provider, http)``: testable seam (fake source in
    tests, real KijijiSource in production).
  - Per-listing try/except isolation; summary counters.
  - Discord POST **outside** the DB transaction; ``alerted_at`` stamped in a
    second short transaction on success (the duplicate-post fix).

Alert logic (Decision 4):
  Fire when:
    - ``price_deal_score >= settings.private_deal_threshold`` OR matched a
      saved search
    - AND ``user_action != 'passed'``
    - AND (``alerted_at`` is None OR price dropped
          >= ``settings.private_realert_drop_pct`` below ``last_alert_price_cad``)
  Dedup: ``alerted_at`` stamps the last successful post; price-drop re-alert
  uses ``last_alert_price_cad``.

``main()`` imports ``KijijiSource`` lazily (Task 5, not yet implemented) so
the module still imports cleanly during Task 4 tests.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import aiohttp
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from carbuyer.apps.notifier.channel_resolver import resolve_channels
from carbuyer.apps.notifier.discord_post import post_simple_message
from carbuyer.apps.private_sale.enrich import enrich_private_listing
from carbuyer.apps.private_sale.value import value_private_listing
from carbuyer.db.enums import EnrichmentStatus, UserAction, ValuationStatus
from carbuyer.db.models import PrivateListing, SavedSearch, SavedSearchMatch
from carbuyer.db.private_upsert import upsert_private_listing
from carbuyer.db.saved_searches import adapt_private_listing, match_listing
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock
from carbuyer.sources.base import RawPrivateListing

log = get_logger("private_sale.worker")

_PRIVATE_CHANNEL_KEY = "private_deals"
_FALLBACK_KEY = "early_warning"


# ─── Source protocol ─────────────────────────────────────────────────────────

@runtime_checkable
class PrivateListingSource(Protocol):
    """Duck-type interface for a classifieds scraper source.

    ``iter_search_results`` yields raw listings from a search page walk
    (pagination handled inside). ``fetch_listing_detail`` enriches a result
    with detail-page data; it may return the same object if the search result
    already has sufficient data.
    """

    async def iter_search_results(
        self, *, provinces: tuple[str, ...] = (),
    ) -> AsyncGenerator[RawPrivateListing, None]: ...

    async def fetch_listing_detail(
        self, raw: RawPrivateListing,
    ) -> RawPrivateListing: ...


# ─── Channel resolution ───────────────────────────────────────────────────────

async def _resolve_private_channel() -> int | None:
    """Resolve the private_deals channel (fallback: early_warning).

    Returns None when neither key is configured — the caller skips alerting.
    """
    resolved = await resolve_channels(
        settings.discord_channels,
        guild_id=settings.discord_guild_id,
        bot_token=settings.discord_bot_token,
    )
    return resolved.get(_PRIVATE_CHANNEL_KEY) or resolved.get(_FALLBACK_KEY)


# ─── Alert helpers ────────────────────────────────────────────────────────────

def _should_alert(
    listing: PrivateListing,
    *,
    matched: bool,
) -> bool:
    """Return True if this listing warrants a Discord alert this cycle."""
    is_deal = (
        listing.price_deal_score is not None
        and listing.price_deal_score >= settings.private_deal_threshold
    )
    if not (is_deal or matched):
        return False
    if listing.user_action == UserAction.PASSED.value:
        return False
    if listing.alerted_at is None:
        return True
    # Already alerted — re-alert only on a significant price drop.
    if (
        listing.last_alert_price_cad is not None
        and listing.ask_price_cad is not None
    ):
        threshold = listing.last_alert_price_cad * Decimal(
            str(1 - settings.private_realert_drop_pct)
        )
        return listing.ask_price_cad <= threshold
    return False


def _compose_alert(
    listing: PrivateListing,
    *,
    matching_search_names: list[str],
) -> str:
    """Build the plaintext Discord message for a private-sale alert."""
    vehicle = " ".join(
        str(p) for p in [listing.year, listing.make, listing.model, listing.trim] if p
    ) or listing.title or "Unknown vehicle"
    ask_str = (
        f"${listing.ask_price_cad:,.0f}"
        if listing.ask_price_cad is not None
        else "price unknown"
    )
    ev_str = (
        f"${listing.expected_value_cad:,.0f}"
        if listing.expected_value_cad is not None
        else "n/a"
    )
    deal_str = (
        f"{listing.price_deal_score:+.0%}"
        if listing.price_deal_score is not None
        else "n/a"
    )
    condition = listing.condition_categorical or "unknown"
    parts = [
        f"**{vehicle}**",
        f"Ask: {ask_str}  |  Expected: {ev_str}  |  Deal score: {deal_str}",
        f"Condition: {condition}",
    ]
    if matching_search_names:
        parts.append(f"Matches: {', '.join(matching_search_names)}")
    parts.append(listing.url)
    return "\n".join(parts)


# ─── Per-listing pipeline ─────────────────────────────────────────────────────

async def _process_listing(
    listing_id: int,
    *,
    now: datetime,
    provider: Any,
    http: aiohttp.ClientSession,
    channel_id: int | None,
    counts: dict[str, int],
) -> None:
    """Enrich → value → match → alert one listing. Modifies ``counts`` in place."""
    matching_search_names: list[str] = []
    matched = False

    async with get_session() as s, s.begin():
        listing = await s.get(PrivateListing, listing_id)
        if listing is None:
            return

        if listing.enrichment_status == EnrichmentStatus.PENDING.value:
            await enrich_private_listing(listing, provider=provider)
            counts["enriched"] += 1

        if listing.valuation_status == ValuationStatus.PENDING.value:
            await value_private_listing(s, listing)
            counts["valued"] += 1

        active_searches: list[SavedSearch] = list(
            (
                await s.execute(
                    select(SavedSearch).where(SavedSearch.is_active.is_(True))
                )
            ).scalars().all()
        )
        matchable = adapt_private_listing(listing)
        for search in active_searches:
            if match_listing(matchable, search):
                await s.execute(
                    pg_insert(SavedSearchMatch)
                    .values(
                        saved_search_id=search.id,
                        source_kind="private_listing",
                        source_id=listing.id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["saved_search_id", "source_kind", "source_id"],
                    )
                )
                matching_search_names.append(search.name)
                matched = True
        if matched:
            counts["matched"] += 1

        should = _should_alert(listing, matched=matched)
        ask_at_decision = listing.ask_price_cad
    # tx closed.

    if not should or channel_id is None:
        return

    # Phase 2: POST outside the transaction.
    content = _compose_alert(listing, matching_search_names=matching_search_names)
    ok = await post_simple_message(channel_id, content, session=http)
    if not ok:
        counts["post_failed"] += 1
        log.warning("private_sale: Discord post failed", listing_id=listing_id)
        return  # leave alerted_at NULL → retried next cycle

    # Phase 3: stamp alerted_at in a second short tx.
    # No ``alerted_at is None`` guard here (unlike the digest runner): the
    # private worker re-alerts on price drops, so it must re-stamp both fields
    # to update the baseline for the *next* price-drop check.
    async with get_session() as s2, s2.begin():
        l2 = await s2.get(PrivateListing, listing_id)
        if l2 is not None:
            l2.alerted_at = now
            l2.last_alert_price_cad = ask_at_decision
    counts["alerted"] += 1


# ─── Core cycle ──────────────────────────────────────────────────────────────

async def run_cycle(
    now: datetime,
    *,
    source: Any,
    provider: Any,
    http: aiohttp.ClientSession,
) -> dict[str, int]:
    """One scrape→process→alert pass. Injected ``source`` and ``provider`` allow
    the test suite to supply a fake source and a mocked LLM provider without any
    network I/O.

    Returns a counter dict: upserted, enriched, valued, matched, alerted,
    post_failed, errors.
    """
    counts: dict[str, int] = {
        "upserted": 0,
        "enriched": 0,
        "valued": 0,
        "matched": 0,
        "alerted": 0,
        "post_failed": 0,
        "errors": 0,
    }

    # Step 1: scrape + upsert.
    async for raw_result in source.iter_search_results(provinces=settings.private_provinces):
        try:
            raw = await source.fetch_listing_detail(raw_result)
            async with get_session() as s, s.begin():
                await upsert_private_listing(s, raw)
            counts["upserted"] += 1
        except Exception:
            sid = getattr(raw_result, "source_listing_id", "?")
            log.exception("private_sale: upsert failed", source_listing_id=sid)
            counts["errors"] += 1

    # Step 2: select pending listings.
    async with get_session() as s:
        pending_ids: list[int] = list(
            (
                await s.execute(
                    select(PrivateListing.id).where(
                        or_(
                            PrivateListing.enrichment_status
                            == EnrichmentStatus.PENDING.value,
                            PrivateListing.valuation_status == ValuationStatus.PENDING.value,
                        )
                    )
                )
            ).scalars().all()
        )

    if not pending_ids:
        log.info("private_sale: no pending listings")
        return counts

    channel_id = await _resolve_private_channel()
    if channel_id is None:
        log.warning("private_sale: no Discord channel configured")

    # Step 3: per-listing enrich → value → match → alert.
    for listing_id in pending_ids:
        try:
            await _process_listing(
                listing_id,
                now=now,
                provider=provider,
                http=http,
                channel_id=channel_id,
                counts=counts,
            )
        except Exception:
            log.exception(
                "private_sale: listing processing failed", listing_id=listing_id,
            )
            counts["errors"] += 1

    log.info("private_sale: cycle complete", **counts)
    return counts


# ─── Entry point ─────────────────────────────────────────────────────────────

async def main(now: datetime | None = None) -> None:
    """Singleton-locked entry point. Imports KijijiSource lazily (Task 5)."""
    lock_conn = await acquire_singleton_lock("private_sale")
    try:
        if now is None:
            now = datetime.now(UTC)

        from carbuyer.llm.openai_provider import OpenAIProvider  # noqa: PLC0415
        from carbuyer.sources.kijiji.source import KijijiSource  # noqa: PLC0415

        source: Any = KijijiSource()
        provider = OpenAIProvider()

        async with aiohttp.ClientSession() as http_session:
            await run_cycle(now, source=source, provider=provider, http=http_session)
    finally:
        await lock_conn.close()
