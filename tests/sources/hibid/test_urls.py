from carbuyer.sources.hibid.urls import (
    catalog_url,
    lot_url,
    province_lots_url,
    province_vehicles_url,
)


def test_province_vehicles_url() -> None:
    # Catalogs/events view — used by auction-discoverer to enumerate auctions.
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
