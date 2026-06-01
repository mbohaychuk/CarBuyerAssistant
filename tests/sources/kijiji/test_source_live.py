"""Opt-in live Kijiji scrape — run with RUN_LIVE_SCRAPE_TESTS=1.

Skipped by default (network + Kijiji's ToS + flakiness). As of 2026-05-30 plain
requests succeed (no Cloudflare challenge), unlike HiBid. This is the canary
that tells us when Kijiji re-platforms and the __NEXT_DATA__ parser needs
re-capturing.
"""
from __future__ import annotations

import os

import pytest

from carbuyer.sources.base import RawPrivateListing
from carbuyer.sources.kijiji.source import KijijiSource

_SAMPLE_SIZE = 3
_LIVE = os.getenv("RUN_LIVE_SCRAPE_TESTS") == "1"
_skip = pytest.mark.skipif(
    not _LIVE,
    reason="live scrape — opt in with RUN_LIVE_SCRAPE_TESTS=1",
)


@_skip
async def test_live_iter_search_results_alberta() -> None:
    async with KijijiSource() as src:
        results: list[RawPrivateListing] = []
        async for raw in src.iter_search_results(provinces=("AB",)):
            results.append(raw)
            if len(results) >= _SAMPLE_SIZE:
                break
    assert results
    first = results[0]
    assert first.source == "kijiji"
    assert first.source_listing_id
    assert first.url.startswith("https://www.kijiji.ca/")
    assert first.pickup_province == "AB"


@_skip
async def test_live_fetch_listing_detail_enriches() -> None:
    async with KijijiSource() as src:
        stub: RawPrivateListing | None = None
        async for raw in src.iter_search_results(provinces=("AB",)):
            # Skip "Please Contact" rows so we land on one with full data.
            if raw.ask_price_cad is not None:
                stub = raw
                break
        assert stub is not None
        detail = await src.fetch_listing_detail(stub)
    # The detail page should carry at least as many photos as the search stub.
    assert len(detail.photos) >= len(stub.photos)
    assert detail.source_listing_id == stub.source_listing_id
