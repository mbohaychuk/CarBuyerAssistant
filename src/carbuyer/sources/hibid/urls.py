from __future__ import annotations

# HiBid province slug per Canadian province/territory code. All follow the same
# lowercase-hyphenated pattern (ontario + quebec verified live). Which of these
# the discoverer actually walks is set by `settings.hibid_provinces`.
PROVINCE_PATH: dict[str, str] = {
    "AB": "alberta",
    "BC": "british-columbia",
    "SK": "saskatchewan",
    "MB": "manitoba",
    "ON": "ontario",
    "QC": "quebec",
    "NS": "nova-scotia",
    "NB": "new-brunswick",
    "NL": "newfoundland-and-labrador",
    "PE": "prince-edward-island",
    "YT": "yukon",
    "NT": "northwest-territories",
    "NU": "nunavut",
}

# HiBid's "Cars & Vehicles" category id.
CARS_VEHICLES_CATEGORY = "700006"


def province_vehicles_url(province: str) -> str:
    """URL for the catalogs/events view (auctions hosting vehicle lots)."""
    path = PROVINCE_PATH[province]
    return (
        f"https://hibid.com/{path}/auctions/{CARS_VEHICLES_CATEGORY}"
        f"/cars-and-vehicles?status=open"
    )


def province_lots_url(province: str) -> str:
    """URL for the per-lot view (individual lots flattened across auctions)."""
    path = PROVINCE_PATH[province]
    return (
        f"https://hibid.com/{path}/lots/{CARS_VEHICLES_CATEGORY}"
        f"/cars-and-vehicles?status=open"
    )


def lot_url(lot_id: str, slug: str = "") -> str:
    if slug:
        return f"https://hibid.com/lot/{lot_id}/{slug}"
    return f"https://hibid.com/lot/{lot_id}"


def catalog_url(auction_id: str, slug: str = "") -> str:
    if slug:
        return f"https://hibid.com/catalog/{auction_id}/{slug}"
    return f"https://hibid.com/catalog/{auction_id}"
