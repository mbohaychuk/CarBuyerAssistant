"""Kijiji private-listing source.

Pulls a want's Cars & Trucks search page and parses its schema.org ld+json
ItemList into ``RawListing``s (see ``parser.py``). The client lifecycle mirrors
``McDougallSource``: production wires a ``RetryTransport`` around
``httpx.AsyncHTTPTransport`` in ``__aenter__``; tests inject an
``httpx.MockTransport``. Legal rules (carried from research): store only derived
metadata + the source URL (deep-link), never the listing photos (Trader v.
CarGurus) and never seller PII (PIPEDA).
"""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, ClassVar, Self
from urllib.parse import quote

import httpx

from carbuyer.sources.base import ListingSource, RawListing, SourceType, register
from carbuyer.sources.http import jittered_sleep, make_client
from carbuyer.sources.kijiji.parser import parse_search_listings
from carbuyer.sources.retry import RetryTransport
from carbuyer.wants.criteria import search_keywords

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from carbuyer.wants.criteria import WantCriteria

_SEARCH_BASE = "https://www.kijiji.ca/b-cars-trucks/canada"


def _search_url(keyword: str) -> str:
    # Kijiji classic puts the query in the path: /<keyword>/k0c174l0 (verified
    # live — a `?keyword=` query is silently ignored and returns all cars). Each
    # token is percent-encoded so a '/'-bearing term (e.g. "3/4 ton") can't
    # escape the keyword segment.
    slug = "+".join(quote(part.lower(), safe="") for part in keyword.split())
    return f"{_SEARCH_BASE}/{slug}/k0c174l0"


class KijijiSource(ListingSource):
    name: ClassVar[str] = "kijiji"
    version: ClassVar[str] = "1.0"
    kind: ClassVar[SourceType] = "listing"

    def __init__(self, *, _transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._injected_transport = _transport
        self._client_cm: AbstractAsyncContextManager[httpx.AsyncClient] | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        transport = self._injected_transport or RetryTransport(httpx.AsyncHTTPTransport())
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

    async def search_listings(self, criteria: WantCriteria) -> AsyncIterator[RawListing]:
        # One search per targeted make+model; dedupe across searches by ad id so
        # a car matching two keywords is yielded once. A want with no make/model
        # (nor model_specs) searches nothing rather than the whole site.
        seen: set[str] = set()
        for i, keyword in enumerate(search_keywords(criteria)):
            if i:
                await jittered_sleep()
            resp = await self._http.get(_search_url(keyword))
            resp.raise_for_status()
            for listing in parse_search_listings(resp.text):
                if listing.ref.source_listing_id in seen:
                    continue
                seen.add(listing.ref.source_listing_id)
                yield listing


register(KijijiSource())
