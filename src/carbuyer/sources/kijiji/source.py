"""KijijiSource stub — placeholder until Task 5 (HTML fixtures required).

The worker's ``main()`` imports this lazily. Replace this stub with the real
implementation once Kijiji HTML fixtures are captured (see Task 5 in the plan).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from carbuyer.sources.base import RawPrivateListing


class KijijiSource:
    """Not yet implemented — Task 5 depends on captured HTML fixtures."""

    async def iter_search_results(
        self, *, provinces: tuple[str, ...] = (),
    ) -> AsyncIterator[RawPrivateListing]:
        raise NotImplementedError("KijijiSource requires Task 5 implementation")
        yield  # pyright: ignore[reportUnreachable]

    async def fetch_listing_detail(
        self, raw: RawPrivateListing,
    ) -> RawPrivateListing:
        raise NotImplementedError("KijijiSource requires Task 5 implementation")
