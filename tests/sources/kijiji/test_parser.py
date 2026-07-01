"""Kijiji SRP parser: schema.org ld+json ItemList -> RawListing. Proven against
a captured 'Cars & Trucks' search page; no network."""
from __future__ import annotations

import pathlib
import re
from decimal import Decimal

from carbuyer.sources.kijiji.parser import parse_search_listings

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "search_cars_canada.html"
_PHONE = re.compile(r"\d{3}[\s.\-]?\d{3}[\s.\-]?\d{4}")


def _listings() -> list:
    return parse_search_listings(_FIXTURE.read_text())


def test_parses_every_car_in_the_itemlist() -> None:
    assert len(_listings()) == 46  # noqa: PLR2004 -- fixture has 46 cards


def test_first_listing_fields_mapped() -> None:
    first = next(x for x in _listings() if x.ref.source_listing_id == "1739451961")
    assert first.ref.source == "kijiji"
    assert first.ref.url == (
        "https://www.kijiji.ca/v-cars-trucks/calgary/2017-ford-escape-se/1739451961"
    )
    assert first.title == "2017 Ford Escape SE"
    assert first.year == 2017  # noqa: PLR2004
    assert first.make == "ford"
    assert first.model == "escape"
    assert first.trim == "SE"
    assert first.mileage_km == 183711  # noqa: PLR2004
    assert first.vin == "1FMCU9GD5HUC36108"
    assert first.asking_price_cad == Decimal("9999")
    assert first.seller_type == "dealer"
    assert first.photos and first.photos[0].startswith("https://media.kijiji.ca/")
    assert first.extra.get("city") == "calgary"


def test_seller_type_derived_from_image_kind() -> None:
    kinds = {x.seller_type for x in _listings()}
    assert "private" in kinds  # ca-prod-fsbo-ads
    assert "dealer" in kinds   # ca-prod-dealer-ads


def test_missing_vin_maps_to_none_without_dropping_the_listing() -> None:
    listings = _listings()
    # VIN is present on only ~1/3 of real listings; the rest must survive as None.
    assert any(x.vin is None for x in listings)
    assert all(x.ref.source_listing_id for x in listings)  # every card keeps a stable id


def test_prices_are_cad_decimals_or_none() -> None:
    for x in _listings():
        assert x.asking_price_cad is None or isinstance(x.asking_price_cad, Decimal)


def test_empty_html_yields_nothing() -> None:
    assert parse_search_listings("<html><head></head><body></body></html>") == []


def test_no_seller_phone_survives_in_description() -> None:
    # 5 of the 46 real listings embed a seller phone in the free-text blurb;
    # RawListing rule 3 / PIPEDA forbids storing it.
    for x in _listings():
        if x.description:
            assert not _PHONE.search(x.description), x.description
