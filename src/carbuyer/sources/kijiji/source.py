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

import httpx

from carbuyer.sources.base import ListingSource, RawListing, SourceType, register
from carbuyer.sources.http import make_client
from carbuyer.sources.kijiji.parser import parse_search_listings
from carbuyer.sources.retry import RetryTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from carbuyer.wants.criteria import WantCriteria


class KijijiSource(ListingSource):
    name: ClassVar[str] = "kijiji"
    version: ClassVar[str] = "1.0"
    kind: ClassVar[SourceType] = "listing"

    _SEARCH_BASE = "https://www.kijiji.ca/b-cars-trucks/canada"

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

    def _search_url(self, criteria: WantCriteria) -> str:
        # Kijiji classic puts the query in the path: /<keyword>/k0c174l0 (verified
        # live — a `?keyword=` query is silently ignored and returns all cars).
        terms = [t.strip() for t in (criteria.makes[:1] + criteria.models[:1]) if t.strip()]
        if not terms:
            return f"{self._SEARCH_BASE}/c174l0"
        keyword = "+".join(terms).replace(" ", "+").lower()
        return f"{self._SEARCH_BASE}/{keyword}/k0c174l0"

    async def search_listings(self, criteria: WantCriteria) -> AsyncIterator[RawListing]:
        resp = await self._http.get(self._search_url(criteria))
        resp.raise_for_status()
        for listing in parse_search_listings(resp.text):
            yield listing


register(KijijiSource())
