"""Kijiji private-listing source (Phase 1 S4 — scaffold).

Registered and contract-complete, but NOT yet implemented: turning a Kijiji
search page into ``RawListing``s needs real sample pages, and fetching a live
commercial site to harvest them is out of scope for an autonomous build. So
``search_listings`` raises ``NotImplementedError`` BEFORE any network I/O — the
scaffold never touches the live site. When sample pages exist, implement the
fetch (via ``carbuyer.sources.curl_transport.CurlCffiTransport`` under
``RetryTransport`` / ``make_client``) + parse here.

The ``_search_url`` criteria→URL mapping is the one part pinnable without
samples, so it's implemented + unit-tested. Legal rules carried from research:
store only derived metadata + the source URL (deep-link), never the listing
photos (Trader v. CarGurus) and never seller PII (PIPEDA).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, ClassVar

from carbuyer.sources.base import ListingSource, RawListing, SourceType, register

if TYPE_CHECKING:
    from carbuyer.wants.criteria import WantCriteria


class KijijiSource(ListingSource):
    name: ClassVar[str] = "kijiji"
    version: ClassVar[str] = "0.1"
    kind: ClassVar[SourceType] = "listing"

    _SEARCH_BASE = "https://www.kijiji.ca/b-cars-trucks/canada"

    def _search_url(self, criteria: WantCriteria) -> str:
        # ponytail: provisional URL shape — verify against live Kijiji when the
        # parser is implemented. The criteria→URL mapping is the part pinnable
        # without sample pages; the exact Kijiji query grammar is not yet fixed.
        terms = [t for t in (criteria.makes[:1] + criteria.models[:1]) if t]
        keyword = " ".join(terms).strip()
        url = f"{self._SEARCH_BASE}/c174l0"
        if keyword:
            url = f"{url}?keyword={keyword.replace(' ', '+')}"
        return url

    async def search_listings(self, criteria: WantCriteria) -> AsyncIterator[RawListing]:
        raise NotImplementedError(
            "Kijiji result selectors pending real sample pages (Phase 1 S4); "
            "implement fetch + parse here, then yield RawListings.",
        )
        yield  # pragma: no cover -- unreachable; marks this an async generator


register(KijijiSource())
