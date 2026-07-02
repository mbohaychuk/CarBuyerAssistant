"""Kijiji source: registration, criteria->keyword/URL grammar, and fetch+parse
wired over an injected transport (no live site)."""
from __future__ import annotations

import pathlib

import httpx
import pytest

from carbuyer.sources.base import SOURCES
from carbuyer.sources.kijiji import KijijiSource
from carbuyer.sources.kijiji.source import _search_url
from carbuyer.wants.criteria import ModelSpec, WantCriteria, search_keywords

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "search_cars_canada.html"


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def test_kijiji_is_registered_as_a_listing_source() -> None:
    src = SOURCES.get("kijiji")
    assert src is not None
    assert src.kind == "listing"


def test_search_url_uses_encoded_keyword_path_grammar() -> None:
    assert (
        _search_url("Nissan Xterra")
        == "https://www.kijiji.ca/b-cars-trucks/canada/nissan+xterra/k0c174l0"
    )


def test_search_url_percent_encodes_path_significant_terms() -> None:
    # "3/4 ton" must not inject extra path segments and break the grammar.
    url = _search_url("3/4 ton")
    assert url == "https://www.kijiji.ca/b-cars-trucks/canada/3%2F4+ton/k0c174l0"
    assert url.count("/k0c174l0") == 1


def test_keywords_prefix_single_make_to_each_model() -> None:
    kws = search_keywords(WantCriteria(makes=["Toyota"], models=["4Runner", "Tacoma"]))
    assert kws == ["Toyota 4Runner", "Toyota Tacoma"]


def test_keywords_from_model_specs_pair_make_and_model() -> None:
    kws = search_keywords(
        WantCriteria(
            model_specs=[
                ModelSpec(make="Lexus", model="GX 470"),
                ModelSpec(make="Toyota", model="4Runner"),
            ],
        ),
    )
    assert kws == ["Lexus GX 470", "Toyota 4Runner"]


def test_keywords_empty_when_no_make_model_or_spec() -> None:
    assert search_keywords(WantCriteria()) == []


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


async def test_search_listings_searches_every_keyword_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("carbuyer.sources.kijiji.source.jittered_sleep", _noop)
    html = _FIXTURE.read_text()
    urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, text=html)

    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        listings = [
            x
            async for x in src.search_listings(
                WantCriteria(makes=["Toyota"], models=["4Runner", "Tacoma"]),
            )
        ]

    assert len(urls) == 2  # noqa: PLR2004 -- one fetch per model
    assert "toyota+4runner" in urls[0]
    assert "toyota+tacoma" in urls[1]
    # both fetches return the same fixture -> dedup by ad id -> 46, not 92
    assert len({x.ref.source_listing_id for x in listings}) == 46  # noqa: PLR2004
    assert len(listings) == 46  # noqa: PLR2004


async def test_search_listings_without_keywords_does_not_fetch() -> None:
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="")

    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        listings = [x async for x in src.search_listings(WantCriteria())]
    assert listings == []
    assert called is False  # empty criteria -> never the all-cars page


async def test_search_listings_tolerates_a_blocked_or_empty_page() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>no results</body></html>")

    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        listings = [x async for x in src.search_listings(WantCriteria(makes=["Nissan"]))]
    assert listings == []
