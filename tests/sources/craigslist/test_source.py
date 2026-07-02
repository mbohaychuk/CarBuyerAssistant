"""Craigslist source: registration, region/URL grammar, and fetch+parse wired
over an injected transport (no live site)."""
from __future__ import annotations

import pathlib

import httpx
import pytest

from carbuyer.shared.config import settings
from carbuyer.sources.base import SOURCES
from carbuyer.sources.craigslist import CraigslistSource
from carbuyer.sources.craigslist.source import _REGION_PROVINCE, _regions_for, _search_url
from carbuyer.wants.criteria import WantCriteria

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "search_owner_tacoma.json"


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def test_craigslist_is_registered_as_a_listing_source() -> None:
    src = SOURCES.get("craigslist")
    assert src is not None
    assert src.kind == "listing"


def test_search_url_targets_by_owner_sapi_with_encoded_query() -> None:
    url = _search_url("vancouver", "Toyota Tacoma")
    assert url.startswith("https://sapi.craigslist.org/web/v8/postings/search/full?")
    assert "purveyor=owner" in url
    assert "cat=cta" in url
    assert "searchPath=area%2Fvancouver" in url
    assert "query=Toyota+Tacoma" in url


def test_regions_filtered_by_want_provinces() -> None:
    configured = ["calgary", "vancouver", "toronto"]  # AB, BC, ON
    assert _regions_for(WantCriteria(provinces=["BC"]), configured) == ["vancouver"]
    # no province filter -> all configured regions
    assert _regions_for(WantCriteria(), configured) == configured


def test_region_province_map_covers_the_default_regions() -> None:
    # Every default configured region must map to a province (else province=None).
    for region in settings.craigslist_regions:
        assert region in _REGION_PROVINCE


async def test_search_listings_fetches_per_region_and_keyword_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("carbuyer.sources.craigslist.source.jittered_sleep", _noop)
    payload = _FIXTURE.read_text()
    urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, text=payload)

    src = CraigslistSource(regions=["vancouver"], _transport=httpx.MockTransport(handler))
    async with src:
        listings = [
            x
            async for x in src.search_listings(
                WantCriteria(makes=["Toyota"], models=["Tacoma"], provinces=["BC"]),
            )
        ]

    assert len(urls) == 1  # one region x one keyword
    assert "searchPath=area%2Fvancouver" in urls[0]
    assert len(listings) == 20  # noqa: PLR2004
    assert all(x.ref.source == "craigslist" for x in listings)
    assert all(x.seller_type == "private" for x in listings)
    assert all(x.location_province == "BC" for x in listings)


async def test_search_listings_without_keywords_does_not_fetch() -> None:
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="{}")

    src = CraigslistSource(regions=["vancouver"], _transport=httpx.MockTransport(handler))
    async with src:
        listings = [x async for x in src.search_listings(WantCriteria())]
    assert listings == []
    assert called is False


async def test_search_listings_skips_regions_outside_want_provinces() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not fetch a region outside the want's provinces")

    src = CraigslistSource(regions=["calgary"], _transport=httpx.MockTransport(handler))
    async with src:  # calgary is AB; want is BC -> no region to search -> no fetch
        listings = [
            x async for x in src.search_listings(WantCriteria(makes=["Toyota"], provinces=["BC"]))
        ]
    assert listings == []
