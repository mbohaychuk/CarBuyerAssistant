"""Craigslist private-listing source.

Queries Craigslist's by-owner search JSON API (``sapi.craigslist.org``) per
region and parses each response into ``RawListing``s (see ``parser.py``). The
client lifecycle mirrors ``McDougallSource``/``KijijiSource``; the API needs a
browser-like ``referer``/``origin`` (set here), no cookie. Craigslist is
per-city, so a want is searched across the configured regions — filtered to the
want's provinces when set. Legal (carried from research): store only derived
metadata + the deep-link URL, never photos (craigslist v. 3Taps / Trader v.
CarGurus) or seller PII (PIPEDA).
"""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, ClassVar, Self
from urllib.parse import urlencode

import httpx

from carbuyer.shared.config import settings
from carbuyer.sources.base import ListingSource, RawListing, SourceType, register
from carbuyer.sources.craigslist.parser import parse_search_results
from carbuyer.sources.http import jittered_sleep, make_client
from carbuyer.sources.retry import RetryTransport
from carbuyer.wants.criteria import search_keywords

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from types import TracebackType

    from carbuyer.wants.criteria import WantCriteria

_SAPI = "https://sapi.craigslist.org/web/v8/postings/search/full"
_HEADERS = {"referer": "https://www.craigslist.org/", "origin": "https://www.craigslist.org"}

# Canadian craigslist regions (subdomains) -> province. Small, static set; the
# configured `craigslist_regions` chooses which to search, this gives each a
# province for want matching + landed-cost.
_REGION_PROVINCE: dict[str, str] = {
    "calgary": "AB", "edmonton": "AB",
    "vancouver": "BC", "victoria": "BC", "kelowna": "BC",
    "winnipeg": "MB",
    "toronto": "ON", "ottawa": "ON", "hamilton": "ON", "kitchener": "ON",
    "london": "ON", "windsor": "ON",
    "montreal": "QC",
    "halifax": "NS",
    "saskatoon": "SK", "regina": "SK",
}


def _regions_for(criteria: WantCriteria, configured: Sequence[str]) -> list[str]:
    """Regions to search: configured order, narrowed to the want's provinces when
    it sets any (else all configured)."""
    if criteria.provinces:
        want = {p.upper() for p in criteria.provinces}
        return [r for r in configured if _REGION_PROVINCE.get(r) in want]
    return list(configured)


def _search_url(region: str, keyword: str) -> str:
    query = urlencode(
        {
            "batch": "0-0-360-0-0",
            "cat": "cta",
            "purveyor": "owner",
            "query": keyword,
            "searchPath": f"area/{region}",
            "lang": "en",
            "cc": "us",
        },
    )
    return f"{_SAPI}?{query}"


class CraigslistSource(ListingSource):
    name: ClassVar[str] = "craigslist"
    version: ClassVar[str] = "1.0"
    kind: ClassVar[SourceType] = "listing"

    def __init__(
        self,
        *,
        regions: Sequence[str] | None = None,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._regions = list(regions) if regions is not None else list(_REGION_PROVINCE)
        self._injected_transport = _transport
        self._client_cm: AbstractAsyncContextManager[httpx.AsyncClient] | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        transport = self._injected_transport or RetryTransport(httpx.AsyncHTTPTransport())
        self._client_cm = make_client(transport=transport, headers=_HEADERS)
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
                "CraigslistSource used outside `async with` — wrap in context manager",
            )
        return self._client

    async def search_listings(self, criteria: WantCriteria) -> AsyncIterator[RawListing]:
        keywords = search_keywords(criteria)
        if not keywords:
            return
        seen: set[str] = set()
        first = True
        for region in _regions_for(criteria, self._regions):
            province = _REGION_PROVINCE.get(region)
            for keyword in keywords:
                if not first:
                    await jittered_sleep()
                first = False
                resp = await self._http.get(_search_url(region, keyword))
                resp.raise_for_status()
                payload: Any = resp.json()
                for listing in parse_search_results(
                    payload, region=region, province=province,
                ):
                    if listing.ref.source_listing_id in seen:
                        continue
                    seen.add(listing.ref.source_listing_id)
                    yield listing


register(CraigslistSource(regions=list(settings.craigslist_regions)))
