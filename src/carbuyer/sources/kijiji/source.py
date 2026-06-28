"""Kijiji private-listing source (Phase 1 S4 — scaffold).

The fetch substrate is real: a want's criteria → a Kijiji search URL → an HTTP
GET through the curl_cffi TLS-impersonation transport (under RetryTransport),
optionally via a CA residential proxy. The HTML→``RawListing`` extraction is
deliberately NOT implemented: correct selectors require real Kijiji sample
pages, and fetching a live commercial site to harvest them is out of scope for
an autonomous build. ``_parse_listings`` raises ``NotImplementedError`` until
sample pages are wired into a fixture.

Legal rules carried from the design research: store only derived metadata +
the source URL (deep-link), never the listing photos (Trader v. CarGurus) and
never seller PII (PIPEDA).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, ClassVar

import httpx

from carbuyer.shared.config import settings
from carbuyer.sources.base import ListingSource, RawListing, SourceType, register
from carbuyer.sources.curl_transport import CurlCffiTransport
from carbuyer.sources.http import make_client
from carbuyer.sources.retry import RetryTransport

if TYPE_CHECKING:
    from carbuyer.wants.criteria import WantCriteria


class KijijiSource(ListingSource):
    name: ClassVar[str] = "kijiji"
    version: ClassVar[str] = "0.1"
    kind: ClassVar[SourceType] = "listing"

    _SEARCH_BASE = "https://www.kijiji.ca/b-cars-trucks/canada"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Injectable transport for tests (httpx.MockTransport); production builds
        # the curl_cffi impersonation transport under RetryTransport.
        self._transport = transport

    def _client_transport(self) -> httpx.AsyncBaseTransport:
        if self._transport is not None:
            return self._transport
        return RetryTransport(
            CurlCffiTransport(
                impersonate=settings.http_impersonate, proxy=settings.proxy_url,
            ),
        )

    def _search_url(self, criteria: WantCriteria) -> str:
        # ponytail: provisional URL shape — verify the path/params against live
        # Kijiji once real sample pages exist. The criteria→URL mapping is the
        # contract; the exact Kijiji query grammar is not yet pinned.
        terms = [t for t in (criteria.makes[:1] + criteria.models[:1]) if t]
        keyword = " ".join(terms).strip()
        url = f"{self._SEARCH_BASE}/c174l0"
        if keyword:
            url = f"{url}?keyword={keyword.replace(' ', '+')}"
        return url

    def _parse_listings(self, html: str) -> list[RawListing]:
        raise NotImplementedError(
            "Kijiji HTML selectors are pending real sample pages (Phase 1 S4); "
            "wire sample HTML into a fixture, then implement extraction here.",
        )

    async def search_listings(self, criteria: WantCriteria) -> AsyncIterator[RawListing]:
        url = self._search_url(criteria)
        async with make_client(transport=self._client_transport()) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            for raw in self._parse_listings(resp.text):
                yield raw


register(KijijiSource())
