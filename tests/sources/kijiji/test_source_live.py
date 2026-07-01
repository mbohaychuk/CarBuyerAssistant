"""Live end-to-end scrape, opt-in. Unlike HiBid (Cloudflare-403'd), Kijiji
serves plain httpx a 200 with the ld+json intact — verified 2026-07-01, which
returned 38 parsed Nissan Xterra listings through the production transport."""
from __future__ import annotations

import os

import pytest

from carbuyer.sources.kijiji.source import KijijiSource
from carbuyer.wants.criteria import WantCriteria


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_SCRAPE_TESTS") != "1",
    reason="live scrape — opt in with RUN_LIVE_SCRAPE_TESTS=1",
)
async def test_live_search_yields_parsed_listings() -> None:
    async with KijijiSource() as src:
        listings = [
            x
            async for x in src.search_listings(
                WantCriteria(makes=["Nissan"], models=["Xterra"]),
            )
        ]
    assert listings
    first = listings[0]
    assert first.ref.source == "kijiji"
    assert first.ref.source_listing_id
    assert first.ref.url.startswith("https://www.kijiji.ca/")
