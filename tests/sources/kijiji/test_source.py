"""Kijiji source: registration, criteria->URL grammar, and fetch+parse wired
over an injected transport (no live site)."""
from __future__ import annotations

import pathlib

import httpx

from carbuyer.sources.base import SOURCES
from carbuyer.sources.kijiji import KijijiSource
from carbuyer.wants.criteria import WantCriteria

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "search_cars_canada.html"


def test_kijiji_is_registered_as_a_listing_source() -> None:
    src = SOURCES.get("kijiji")
    assert src is not None
    assert src.kind == "listing"


def test_search_url_uses_keyword_path_grammar() -> None:
    url = KijijiSource()._search_url(  # pyright: ignore[reportPrivateUsage]
        WantCriteria(makes=["Nissan"], models=["Xterra"]),
    )
    assert url == "https://www.kijiji.ca/b-cars-trucks/canada/nissan+xterra/k0c174l0"


def test_search_url_without_terms_is_the_category_page() -> None:
    url = KijijiSource()._search_url(WantCriteria())  # pyright: ignore[reportPrivateUsage]
    assert url == "https://www.kijiji.ca/b-cars-trucks/canada/c174l0"


async def test_search_listings_fetches_and_parses() -> None:
    html = _FIXTURE.read_text()
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text=html)

    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        listings = [
            x
            async for x in src.search_listings(
                WantCriteria(makes=["Nissan"], models=["Xterra"]),
            )
        ]

    assert len(listings) == 46  # noqa: PLR2004
    assert all(x.ref.source == "kijiji" and x.ref.source_listing_id for x in listings)
    assert "nissan+xterra" in captured["url"]
    assert "k0c174l0" in captured["url"]


async def test_search_listings_tolerates_a_blocked_or_empty_page() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>no results</body></html>")

    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        listings = [x async for x in src.search_listings(WantCriteria(makes=["Nissan"]))]
    assert listings == []
