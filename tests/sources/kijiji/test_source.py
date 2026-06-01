"""Tests for KijijiSource over an injected httpx.MockTransport.

No network: the search/detail pages are served from the captured fixtures.
``jittered_sleep`` is stubbed (autouse) so multi-page walks don't actually wait.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from carbuyer.shared.config import settings
from carbuyer.sources.base import RawPrivateListing
from carbuyer.sources.kijiji.source import KijijiSource

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_JEEP_ID = "1738329373"
_SPARSE_ID = "1736878962"
_JEEP_URL = (
    "https://www.kijiji.ca/v-cars-trucks/edmonton/"
    "2016-jeep-cherokee-trailhawk-115-km/1738329373"
)

# Expected counts / values derived from the captured fixtures.
_OWNER_PAGE_TOTAL = 45
_MIXED_OWNERS = 7
_DETAIL_PHOTO_COUNT = 15
_MIN_FULL_DESC_LEN = 200
_JEEP_MILEAGE_KM = 115000
_JEEP_YEAR = 2016
_STUB_OVERRIDE_YEAR = 2003   # differs from the Jeep detail title-year (2016)
_WALK_PAGES = 3

Handler = Callable[[httpx.Request], httpx.Response]


def _read(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_sleep(  # pyright: ignore[reportUnusedFunction]  -- used via pytest autouse injection
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _noop(*_args: float) -> None:
        return None

    monkeypatch.setattr("carbuyer.sources.kijiji.source.jittered_sleep", _noop)


def _search_handler(search_fixture: str) -> Handler:
    """Serve *search_fixture* for page 1 of any search, an empty page for any
    ``page-N/`` request, and the matching detail fixture for detail URLs."""
    search_html = _read(search_fixture)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/b-cars-trucks/" in path:
            if "/page-" in path:
                return httpx.Response(200, text="<html><body></body></html>")
            return httpx.Response(200, text=search_html)
        if "/v-cars-trucks/" in path:
            fixture = (
                "listing_detail_sparse.html"
                if _SPARSE_ID in path
                else "listing_detail_jeep_cherokee.html"
            )
            return httpx.Response(200, text=_read(fixture))
        return httpx.Response(404)

    return handler


async def _collect(
    handler: Handler,
    provinces: tuple[str, ...],
) -> list[RawPrivateListing]:
    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        return [raw async for raw in src.iter_search_results(provinces=provinces)]


# ── iter_search_results ────────────────────────────────────────────────────────


async def test_iter_yields_owner_listings_for_province() -> None:
    results = await _collect(_search_handler("search_owner_alberta.html"), ("AB",))
    assert len(results) == _OWNER_PAGE_TOTAL
    assert all(r.source == "kijiji" for r in results)
    # Every AB-search result resolves to AB — including the one whose address
    # omits the province (it falls back to the search province).
    assert all(r.pickup_province == "AB" for r in results)
    jeep = next(r for r in results if r.source_listing_id == _JEEP_ID)
    assert jeep.make == "jeep"
    assert jeep.ask_price_cad == Decimal("13999")
    assert jeep.mileage_km == _JEEP_MILEAGE_KM


async def test_iter_skips_dealers() -> None:
    results = await _collect(_search_handler("search_mixed_alberta.html"), ("AB",))
    # 46 listings on the mixed page, only 7 owners survive the dealer filter.
    assert len(results) == _MIXED_OWNERS


async def test_iter_skips_unsupported_province() -> None:
    # Ontario isn't in the western-Canada scope; no requests, no results.
    results = await _collect(_search_handler("search_owner_alberta.html"), ("ON",))
    assert results == []


async def test_iter_dedups_across_repeated_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Walk up to 3 pages, but serve the SAME owner page for every page number.
    monkeypatch.setattr(settings, "private_max_search_pages", _WALK_PAGES)
    search_html = _read("search_owner_alberta.html")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/b-cars-trucks/" in request.url.path:
            return httpx.Response(200, text=search_html)
        return httpx.Response(404)

    results = await _collect(handler, ("AB",))
    # A page that only repeats already-seen ids ends the walk — no duplicates.
    assert len(results) == _OWNER_PAGE_TOTAL
    assert len({r.source_listing_id for r in results}) == _OWNER_PAGE_TOTAL


async def test_iter_stops_on_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "private_max_search_pages", _WALK_PAGES)
    results = await _collect(_search_handler("search_owner_alberta.html"), ("AB",))
    # page-1 has 45; page-2 is empty -> walk stops, still 45.
    assert len(results) == _OWNER_PAGE_TOTAL


async def test_iter_continues_provinces_on_fetch_failure() -> None:
    search_html = _read("search_owner_alberta.html")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/b-cars-trucks/" in path:
            calls.append(path)
            if "/alberta/" in path:
                return httpx.Response(500)
            return httpx.Response(200, text=search_html)
        return httpx.Response(404)

    results = await _collect(handler, ("AB", "MB"))
    # AB's page errors (yields nothing) but MB still produces results.
    assert len(results) == _OWNER_PAGE_TOTAL
    assert any("/alberta/" in c for c in calls)
    assert any("/manitoba/" in c for c in calls)


# ── fetch_listing_detail ────────────────────────────────────────────────────────


def _jeep_search_stub() -> RawPrivateListing:
    """A search-page stub for the Jeep, as iter_search_results would emit it."""
    return RawPrivateListing(
        source="kijiji",
        source_listing_id=_JEEP_ID,
        url=_JEEP_URL,
        title="2016 Jeep Cherokee Trailhawk  115 KM",
        description="short truncated search description...",
        photos=["p1", "p2", "p3", "p4", "p5"],
        year=2016,
        make="jeep",
        model="cherokee",
        trim="Trailhawk",
        mileage_km=115000,
        vin="1C4PJMCS0GW123456",
        ask_price_cad=Decimal("13999"),
        pickup_province="AB",
        pickup_city="Edmonton",
    )


async def test_fetch_detail_coalesces_search_and_detail() -> None:
    handler = _search_handler("search_owner_alberta.html")
    stub = _jeep_search_stub()
    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        merged = await src.fetch_listing_detail(stub)
    # Detail page wins for the full photo set + long description.
    assert len(merged.photos) == _DETAIL_PHOTO_COUNT
    assert merged.description is not None
    assert len(merged.description) > _MIN_FULL_DESC_LEN
    # Search stub wins for fields the detail page leaves empty.
    assert merged.trim == "Trailhawk"
    assert merged.mileage_km == _JEEP_MILEAGE_KM
    assert merged.year == _JEEP_YEAR
    assert merged.make == "jeep"
    assert merged.vin == "1C4PJMCS0GW123456"  # detail has no vin -> stub preserved
    assert merged.ask_price_cad == Decimal("13999")
    assert merged.source_listing_id == _JEEP_ID


async def test_fetch_detail_keeps_authoritative_search_year() -> None:
    # The search page's normalized caryear is authoritative. The detail page's
    # caryear is always empty, so its year is only a title-regex guess and must
    # NOT override the stub — even when the title year differs from caryear.
    handler = _search_handler("search_owner_alberta.html")
    stub = replace(_jeep_search_stub(), year=_STUB_OVERRIDE_YEAR)
    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        merged = await src.fetch_listing_detail(stub)
    # The Jeep detail title parses to 2016, but the stub's caryear must win.
    assert merged.year == _STUB_OVERRIDE_YEAR


async def test_fetch_detail_returns_stub_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/v-cars-trucks/" in request.url.path:
            return httpx.Response(503)
        return httpx.Response(404)

    stub = _jeep_search_stub()
    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        result = await src.fetch_listing_detail(stub)
    # A failed detail fetch must not drop the listing — stub is returned as-is.
    assert result is stub


async def test_fetch_detail_returns_stub_on_unparseable_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>no next-data</html>")

    stub = _jeep_search_stub()
    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        result = await src.fetch_listing_detail(stub)
    assert result is stub


async def test_fetch_detail_returns_stub_on_malformed_url() -> None:
    # raw.url is scrape-controlled; a malformed URL raises httpx.InvalidURL
    # (not an HTTPError). It must still degrade to the stub, not drop the listing.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(404)

    stub = replace(_jeep_search_stub(), url="https://www.kijiji.ca:notaport/x/1")
    async with KijijiSource(_transport=httpx.MockTransport(handler)) as src:
        result = await src.fetch_listing_detail(stub)
    assert result is stub
