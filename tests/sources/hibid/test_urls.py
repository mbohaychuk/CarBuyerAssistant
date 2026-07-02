import typing

from carbuyer.shared.config import Province
from carbuyer.sources.hibid.urls import (
    PROVINCE_PATH,
    catalog_url,
    lot_url,
    province_lots_url,
    province_vehicles_url,
)


def test_province_vehicles_url() -> None:
    # Catalogs/events view — used by the ingester to enumerate auctions.
    assert (
        province_vehicles_url("AB")
        == "https://hibid.com/alberta/auctions/700006/cars-and-vehicles?status=open"
    )


def test_province_lots_url() -> None:
    # Per-lot listing view — used when scraping lots without a known auction.
    assert (
        province_lots_url("BC")
        == "https://hibid.com/british-columbia/lots/700006/cars-and-vehicles?status=open"
    )


def test_lot_url() -> None:
    assert lot_url("12345", "1995-ford-f150") == "https://hibid.com/lot/12345/1995-ford-f150"


def test_lot_url_no_slug() -> None:
    assert lot_url("12345") == "https://hibid.com/lot/12345"


def test_catalog_url() -> None:
    assert (
        catalog_url("740236", "vehicle-equipment-with-nl-power-auction")
        == "https://hibid.com/catalog/740236/vehicle-equipment-with-nl-power-auction"
    )


def test_province_path_covers_every_province_literal() -> None:
    # Any province valid for settings.hibid_provinces must resolve to a slug,
    # so PROVINCE_PATH[province] can never KeyError in discover_auctions.
    for province in typing.get_args(Province):
        assert province in PROVINCE_PATH
        assert province_vehicles_url(province).startswith("https://hibid.com/")


def test_central_canada_urls() -> None:
    assert (
        province_vehicles_url("ON")
        == "https://hibid.com/ontario/auctions/700006/cars-and-vehicles?status=open"
    )
    assert province_vehicles_url("QC").startswith("https://hibid.com/quebec/")
