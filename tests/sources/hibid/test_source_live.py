import os

import pytest

from carbuyer.sources.base import AuctionRef
from carbuyer.sources.hibid.source import HibidSource


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_SCRAPE_TESTS") != "1",
    reason=(
        "live scrape — opt in with RUN_LIVE_SCRAPE_TESTS=1; "
        "currently 403'd by Cloudflare. Phase 1.5 spike adds a CF-bypass transport."
    ),
)
async def test_live_discover_at_least_one_alberta_auction() -> None:
    """When run with RUN_LIVE_SCRAPE_TESTS=1, discovers >=1 Alberta auction.

    As of 2026-05-09 this fails with HTTP 403 from Cloudflare bot management.
    See Phase 1 overlay in the implementation plan for the deferred bypass spike.
    """
    async with HibidSource(provinces=["AB"]) as src:
        refs: list[AuctionRef] = []
        async for ref in src.discover_auctions():
            refs.append(ref)
            if len(refs) >= 1:
                break
    assert len(refs) >= 1
