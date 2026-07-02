"""Craigslist search-API parser: sapi JSON -> RawListing. Proven against a
captured by-owner Tacoma search (vancouver, 20 postings); no network."""
from __future__ import annotations

import json
import pathlib
from decimal import Decimal

from carbuyer.sources.craigslist.parser import parse_search_results

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "search_owner_tacoma.json"


def _listings(province: str | None = "BC") -> list:
    payload = json.loads(_FIXTURE.read_text())
    return parse_search_results(payload, region="vancouver", province=province)


def test_parses_every_posting() -> None:
    assert len(_listings()) == 20  # noqa: PLR2004


def test_first_posting_fields_mapped() -> None:
    first = next(x for x in _listings() if x.ref.source_listing_id == "7930578957")
    assert first.ref.source == "craigslist"
    assert first.ref.url == (
        "https://vancouver.craigslist.org/van/cto/d/"
        "coquitlam-north-the-nicest-2012-toyota/7930578957.html"
    )
    assert first.title == "The Nicest.. 2012 Toyota Tacoma TRD SPORT 4X4"
    assert first.asking_price_cad == Decimal("14800")
    assert first.mileage_km == 387000  # noqa: PLR2004
    assert first.seller_type == "private"
    assert first.location_province == "BC"
    assert first.photos == []  # legal: never store photos


def test_posting_without_decodable_subarea_uses_region_only_url() -> None:
    # 6/20 lack a decodable subarea; the region-only URL is valid (craigslist
    # redirects it to the canonical subarea URL).
    listing = next(x for x in _listings() if x.ref.source_listing_id == "7943747852")
    assert listing.ref.url == (
        "https://vancouver.craigslist.org/cto/d/"
        "langley-township-northwest-2019-toyota/7943747852.html"
    )


def test_ids_are_stable_digits_and_prices_decimal() -> None:
    for x in _listings():
        assert x.ref.source_listing_id.isdigit()
        assert x.asking_price_cad is None or isinstance(x.asking_price_cad, Decimal)


def test_province_none_when_region_unmapped() -> None:
    assert all(x.location_province is None for x in _listings(province=None))


def test_empty_payload_yields_nothing() -> None:
    assert parse_search_results({}, region="vancouver", province="BC") == []
