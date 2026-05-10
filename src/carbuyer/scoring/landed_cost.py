"""Landed-cost premium model.

Adds the realistic "get the truck home" cost on top of the bid:

- Transport: $/km from origin city centroid plus a $400 dispatch floor;
  the whole transport line clamps at $600 minimum so the model doesn't
  reward picking up locally-listed trash that's still 50 km away.
- Inspection: per-province out-of-province inspection fee (BC AVI $125,
  AB OOP $200, etc.). Falls back to ``DEFAULT_INSPECTION`` for provinces
  not in the table — the actual fee varies, this is a planning estimate.
- Contingency: a flat reserve for "things you only find out after you
  drive it home." Higher in AB/ON because the markets we buy from have
  worse rust / pothole damage history. ``DEFAULT_CONTINGENCY`` for the rest.

Distance fallback: when we don't have city-precise origin and dest, we use
provincial-capital centroid distances. A pair we don't list (e.g. AB↔QC)
returns the cross-country default of 3000 km — directionally right for any
"shipping is going to dominate" check the dashboard wants to do.
"""
from __future__ import annotations

from decimal import Decimal

PROV_INSPECTION: dict[str, int] = {
    "AB": 200, "ON": 120, "BC": 125, "QC": 125,
    "MB": 100, "SK": 75, "NS": 50, "NB": 50,
}
PROV_CONTINGENCY: dict[str, int] = {
    "AB": 350, "ON": 350, "QC": 250,
}
DEFAULT_INSPECTION = 75
DEFAULT_CONTINGENCY = 150
TRANSPORT_DISPATCH_FLOOR = 400
TRANSPORT_MIN_TOTAL = 600
TRANSPORT_PER_KM = 0.65

# Approximate centroid distances between provincial capitals (km). Used as a
# fallback when we don't have the seller's exact city. Pairs not listed get
# CROSS_COUNTRY_DEFAULT_KM — directionally correct for "shipping dominates."
_FALLBACK_DISTANCE: dict[tuple[str, str], int] = {
    ("AB", "AB"): 0, ("AB", "BC"): 1000, ("AB", "SK"): 600, ("AB", "MB"): 1300,
    ("BC", "BC"): 0, ("BC", "AB"): 1000, ("BC", "SK"): 1700, ("BC", "MB"): 2200,
    ("SK", "SK"): 0, ("SK", "AB"): 600, ("SK", "BC"): 1700, ("SK", "MB"): 600,
    ("MB", "MB"): 0, ("MB", "SK"): 600, ("MB", "AB"): 1300, ("MB", "BC"): 2200,
}
CROSS_COUNTRY_DEFAULT_KM = 3000


def distance_km_between(home: str, dest: str) -> int:
    return _FALLBACK_DISTANCE.get((home, dest), CROSS_COUNTRY_DEFAULT_KM)


def landed_cost_premium(*, home: str, dest: str, distance_km: int) -> Decimal:
    """Estimated extra cost to land a vehicle from ``dest`` to ``home``."""
    if home == dest:
        return Decimal("0")
    transport = max(
        TRANSPORT_MIN_TOTAL,
        int(TRANSPORT_DISPATCH_FLOOR + TRANSPORT_PER_KM * distance_km),
    )
    inspection = PROV_INSPECTION.get(dest, DEFAULT_INSPECTION)
    contingency = PROV_CONTINGENCY.get(dest, DEFAULT_CONTINGENCY)
    return Decimal(transport + inspection + contingency)
