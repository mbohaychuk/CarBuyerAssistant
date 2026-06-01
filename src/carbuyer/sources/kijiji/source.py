"""Kijiji private-sale source plugin.

Scrapes kijiji.ca's cars & trucks category, filtered to for-sale-by-owner, for
the private-sale worker. Two-stage like the auction sources: ``iter_search_results``
walks the owner search pages (one ``RawPrivateListing`` stub per result), and
``fetch_listing_detail`` fetches each listing's detail page to fill in the full
description + photo set.

Not registered in ``sources.base.SOURCES``: that registry is consumed by the
auction tooling (ingester, bid_poller, source_watchdog freshness) which keys off
auction tables. A listing source writes ``private_listings`` and is instantiated
directly by ``apps.private_sale``; registering it would only half-integrate it
into auction-shaped tooling. Revisit when the dashboard grows a listing-source
health view.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import ClassVar, Self

import httpx

from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.sources.base import RawPrivateListing, Source, SourceType
from carbuyer.sources.http import jittered_sleep, make_client
from carbuyer.sources.kijiji.parser import (
    KijijiListing,
    parse_listing_detail,
    parse_search_page,
)
from carbuyer.sources.retry import RetryTransport

_log = get_logger("sources.kijiji")

_BASE_URL = "https://www.kijiji.ca"
# Kijiji's cars & trucks category id.
_CATEGORY_ID = 174
# 2-letter province -> (URL slug, Kijiji location id). Verified against live
# Kijiji 2026-05-30. The location id (not the slug) is authoritative — Kijiji
# resolves the page by id and ignores a mismatched slug — so keep them paired.
_PROVINCE_LOCATIONS: dict[str, tuple[str, int]] = {
    "AB": ("alberta", 9003),
    "SK": ("saskatchewan", 9009),
    "MB": ("manitoba", 9006),
}


def _search_url(slug: str, location_id: int, page: int) -> str:
    """Owner-filtered cars & trucks search URL for a province + page.

    ``?for-sale-by=ownr`` is the only owner filter Kijiji honors. Pagination is
    a ``page-N/`` path segment before the ``c<cat>l<loc>`` token.
    """
    page_segment = "" if page <= 1 else f"page-{page}/"
    return (
        f"{_BASE_URL}/b-cars-trucks/{slug}/{page_segment}"
        f"c{_CATEGORY_ID}l{location_id}?for-sale-by=ownr"
    )


class KijijiSource(Source):
    """Source plugin for Kijiji (kijiji.ca) for-sale-by-owner vehicle listings."""

    name: ClassVar[str] = "kijiji"
    # Bump when the parser / scrape contract changes.
    version: ClassVar[str] = "1"
    kind: ClassVar[SourceType] = "listing"

    def __init__(
        self,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Tests inject a MockTransport; production wires a RetryTransport around
        # an httpx.AsyncHTTPTransport in __aenter__.
        self._injected_transport = _transport
        self._client_cm: AbstractAsyncContextManager[httpx.AsyncClient] | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        transport = self._injected_transport or RetryTransport(
            httpx.AsyncHTTPTransport(),
        )
        self._client_cm = make_client(transport=transport)
        self._client = await self._client_cm.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client_cm is not None:
            await self._client_cm.__aexit__(exc_type, exc, tb)
        self._client_cm = None
        self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "KijijiSource used outside `async with` — wrap in context manager",
            )
        return self._client

    async def iter_search_results(
        self,
        *,
        provinces: tuple[str, ...] = (),
    ) -> AsyncIterator[RawPrivateListing]:
        """Walk owner search pages for each province, yielding listing stubs.

        Skips dealer ads (promoted dealer cards can leak onto owner pages) and
        de-duplicates listing ids across pages — a repeat-only page (Kijiji
        clamps an over-large page number to the last page) ends that province's
        walk, as does an empty page or a fetch failure. Bounded by
        ``settings.private_max_search_pages``.
        """
        seen: set[str] = set()
        for code in provinces:
            location = _PROVINCE_LOCATIONS.get(code)
            if location is None:
                _log.warning("unsupported province; skipping", province=code)
                continue
            slug, location_id = location
            for page in range(1, settings.private_max_search_pages + 1):
                url = _search_url(slug, location_id, page)
                try:
                    resp = await self._http.get(url)
                    resp.raise_for_status()
                except Exception as exc:
                    _log.warning(
                        "search page fetch failed; ending province walk",
                        province=code, page=page, url=url, error=str(exc),
                    )
                    break
                entries = parse_search_page(resp.text)
                if not entries:
                    break
                new_this_page = 0
                for entry in entries:
                    if entry.is_dealer or entry.listing_id in seen:
                        continue
                    seen.add(entry.listing_id)
                    new_this_page += 1
                    yield self._to_raw(entry, search_province=code)
                if new_this_page == 0:
                    break
                if page < settings.private_max_search_pages:
                    await jittered_sleep()

    async def fetch_listing_detail(
        self,
        raw: RawPrivateListing,
    ) -> RawPrivateListing:
        """Fetch the detail page for *raw* and merge its richer fields in.

        On any fetch / parse failure the search-page stub is returned unchanged
        — a missing detail page must not drop an otherwise-valid listing.
        """
        try:
            resp = await self._http.get(raw.url)
            resp.raise_for_status()
        except Exception as exc:
            _log.warning(
                "listing detail fetch failed; using search-page data",
                url=raw.url, error=str(exc),
            )
            return raw
        detail = parse_listing_detail(resp.text)
        if detail is None:
            return raw
        return self._merge_detail(raw, detail)

    def _to_raw(
        self,
        entry: KijijiListing,
        *,
        search_province: str,
    ) -> RawPrivateListing:
        return RawPrivateListing(
            source=self.name,
            source_listing_id=entry.listing_id,
            url=entry.url,
            title=entry.title,
            description=entry.description,
            photos=list(entry.photos),
            year=entry.year,
            make=entry.make,
            model=entry.model,
            trim=entry.trim,
            mileage_km=entry.mileage_km,
            vin=None,
            ask_price_cad=entry.ask_price_cad,
            # Address province is best-effort and sometimes absent; the search
            # province is the reliable fallback (we searched within it).
            pickup_province=entry.province or search_province,
            pickup_city=entry.city,
            extra=dict(entry.extra),
        )

    def _merge_detail(
        self,
        stub: RawPrivateListing,
        detail: KijijiListing,
    ) -> RawPrivateListing:
        """Coalesce a detail-page parse over the search stub.

        Detail page wins for the long description + full photo set; the search
        stub wins for normalized year/make/model/mileage (detail's structured
        attributes are usually empty). ``x or stub.x`` keeps the stub's value
        whenever the detail page leaves a field empty.
        """
        return RawPrivateListing(
            source=self.name,
            source_listing_id=stub.source_listing_id,
            url=stub.url,
            title=detail.title or stub.title,
            description=detail.description or stub.description,
            photos=list(detail.photos) or list(stub.photos),
            year=detail.year or stub.year,
            make=detail.make or stub.make,
            model=detail.model or stub.model,
            trim=detail.trim or stub.trim,
            mileage_km=detail.mileage_km or stub.mileage_km,
            vin=stub.vin,
            ask_price_cad=detail.ask_price_cad or stub.ask_price_cad,
            pickup_province=detail.province or stub.pickup_province,
            pickup_city=detail.city or stub.pickup_city,
            extra={**stub.extra, **detail.extra},
        )
