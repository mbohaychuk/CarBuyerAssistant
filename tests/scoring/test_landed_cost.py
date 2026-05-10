from decimal import Decimal

from carbuyer.scoring.landed_cost import (
    DEFAULT_CONTINGENCY,
    DEFAULT_INSPECTION,
    PROV_CONTINGENCY,
    PROV_INSPECTION,
    distance_km_between,
    landed_cost_premium,
)


def test_same_province_is_zero() -> None:
    assert landed_cost_premium(home="AB", dest="AB", distance_km=200) == Decimal("0")


def test_cross_country_includes_inspection_and_contingency() -> None:
    cost = landed_cost_premium(home="AB", dest="ON", distance_km=3500)
    # transport = 400 + 0.65 * 3500 = 2675; max(600, 2675) = 2675
    # inspection ON 120; contingency ON 350.
    expected = Decimal(2675) + Decimal(120) + Decimal(350)
    assert cost == expected


def test_min_floor_applies_to_short_haul() -> None:
    # 100 km would yield 400 + 65 = 465 — below the 600 floor.
    cost = landed_cost_premium(home="AB", dest="BC", distance_km=100)
    expected = Decimal(600) + Decimal(125) + Decimal(150)  # BC inspection 125, default contingency 150
    assert cost == expected


def test_unknown_destination_uses_defaults() -> None:
    cost = landed_cost_premium(home="AB", dest="XX", distance_km=2000)
    transport = 400 + int(0.65 * 2000)
    expected = Decimal(transport) + Decimal(DEFAULT_INSPECTION) + Decimal(DEFAULT_CONTINGENCY)
    assert cost == expected


def test_distance_known_pair() -> None:
    assert distance_km_between("AB", "BC") == 1000


def test_distance_unmapped_pair_uses_fallback() -> None:
    # Unmapped pair (AB↔QC outside our table) returns the cross-country default.
    assert distance_km_between("AB", "QC") == 3000


def test_distance_same_province_is_zero() -> None:
    assert distance_km_between("AB", "AB") == 0


def test_inspection_and_contingency_tables_have_western_canada() -> None:
    # Sanity: the four MVP-target provinces are all in the inspection table.
    for prov in ("AB", "BC", "SK", "MB"):
        assert prov in PROV_INSPECTION
    # Contingency only set for provinces with notably bad pothole / weather
    # repair markets; missing entries default to DEFAULT_CONTINGENCY.
    assert PROV_CONTINGENCY["AB"] >= DEFAULT_CONTINGENCY
