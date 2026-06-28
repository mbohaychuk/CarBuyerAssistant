"""Kijiji source scaffold: registration, criteria→URL, fetch wiring runs, and
the HTML parse is honestly stubbed pending real sample pages."""
from __future__ import annotations

import httpx
import pytest

from carbuyer.sources.base import SOURCES
from carbuyer.sources.kijiji import KijijiSource
from carbuyer.wants.criteria import WantCriteria


def test_kijiji_is_registered_as_a_listing_source() -> None:
    src = SOURCES.get("kijiji")
    assert src is not None
    assert src.kind == "listing"


async def test_search_listings_builds_url_fetches_then_stubbed_parse() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text="<html>kijiji results</html>")

    src = KijijiSource(transport=httpx.MockTransport(handler))
    criteria = WantCriteria(makes=["Nissan"], models=["Xterra"])

    # The fetch wiring runs (the MockTransport captures the request); the
    # HTML→RawListing extraction is stubbed until real sample pages exist.
    with pytest.raises(NotImplementedError, match="selectors"):
        _ = [raw async for raw in src.search_listings(criteria)]

    assert "nissan" in captured["url"].lower()
    assert "xterra" in captured["url"].lower()


async def test_search_listings_propagates_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="blocked")

    src = KijijiSource(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        _ = [raw async for raw in src.search_listings(WantCriteria(makes=["Nissan"]))]
