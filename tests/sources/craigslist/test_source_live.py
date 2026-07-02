"""Live end-to-end scrape, opt-in. Craigslist's by-owner search JSON API serves
plain httpx a 200 with referer/origin headers — verified 2026-07-02, which
returned 20 parsed Tacoma postings through the production transport."""
from __future__ import annotations

import os

import pytest

from carbuyer.sources.craigslist.source import CraigslistSource
from carbuyer.wants.criteria import WantCriteria


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_SCRAPE_TESTS") != "1",
    reason="live scrape — opt in with RUN_LIVE_SCRAPE_TESTS=1",
)
async def test_live_search_yields_parsed_listings() -> None:
    async with CraigslistSource(regions=["vancouver"]) as src:
        listings = [
            x
            async for x in src.search_listings(
                WantCriteria(makes=["Toyota"], models=["Tacoma"], provinces=["BC"]),
            )
        ]
    assert listings
    first = listings[0]
    assert first.ref.source == "craigslist"
    assert first.ref.source_listing_id.isdigit()
    assert first.ref.url.startswith("https://vancouver.craigslist.org/")
    assert first.seller_type == "private"
    assert first.location_province == "BC"
