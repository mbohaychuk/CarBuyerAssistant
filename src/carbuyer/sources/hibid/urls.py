from __future__ import annotations

# Western Canadian provinces. Other provinces (ON, QC, etc.) follow the same
# slug pattern but are out-of-scope for the MVP.
PROVINCE_PATH: dict[str, str] = {
    "AB": "alberta",
    "BC": "british-columbia",
    "SK": "saskatchewan",
    "MB": "manitoba",
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
