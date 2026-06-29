from __future__ import annotations

from decimal import Decimal
from typing import Any

from carbuyer.db.models import AuctionLot
from carbuyer.wants.criteria import ModelSpec, WantCriteria
from carbuyer.wants.matcher import matches


def _lot(**over: Any) -> AuctionLot:
    base: dict[str, Any] = {
        "make": "Nissan",
        "model": "Xterra",
        "year": 2010,
        "trim": None,
        "transmission": "manual",
        "drivetrain": "4wd",
        "mileage_km": 180_000,
        "condition_categorical": "decent",
        "condition_inferred_from_sparse_listing": False,
        "showstopper_flags": [],
    }
    base.update(over)
    return AuctionLot(**base)


def test_empty_criteria_matches_any_clean_lot() -> None:
    assert matches(_lot(), WantCriteria()) is True


def test_make_model_must_match_case_insensitively() -> None:
    want = WantCriteria(makes=["nissan"], models=["xterra"])
    assert matches(_lot(), want) is True
    assert matches(_lot(model="4Runner", make="Toyota"), want) is False


def test_unknown_make_is_excluded_when_make_required() -> None:
    assert matches(_lot(make=None), WantCriteria(makes=["Nissan"])) is False


def test_year_range_enforced_and_unknown_year_excluded() -> None:
    want = WantCriteria(year_min=2005, year_max=2015)
    assert matches(_lot(year=2010), want) is True
    assert matches(_lot(year=2003), want) is False
    assert matches(_lot(year=2020), want) is False
    assert matches(_lot(year=None), want) is False


def test_transmission_manual_only_excludes_automatic_but_keeps_unknown() -> None:
    want = WantCriteria(transmissions=["manual"])
    assert matches(_lot(transmission="manual"), want) is True
    assert matches(_lot(transmission="automatic"), want) is False
    assert matches(_lot(transmission="unknown"), want) is True  # lenient
    assert matches(_lot(transmission=None), want) is True  # lenient


def test_price_ceiling_excludes_over_but_keeps_unknown() -> None:
    want = WantCriteria(price_ceiling_cad=10_000)
    assert matches(_lot(), want, offer_price_cad=Decimal("9000")) is True
    assert matches(_lot(), want, offer_price_cad=Decimal("10000")) is True  # == boundary
    assert matches(_lot(), want, offer_price_cad=Decimal("12000")) is False
    assert matches(_lot(), want, offer_price_cad=None) is True  # lenient
    assert matches(_lot(), want) is True  # price not provided → unknown → lenient


def test_mileage_cap_excludes_over_but_keeps_unknown() -> None:
    want = WantCriteria(max_mileage_km=200_000)
    assert matches(_lot(mileage_km=150_000), want) is True
    assert matches(_lot(mileage_km=260_000), want) is False
    assert matches(_lot(mileage_km=None), want) is True  # lenient


def test_province_filter_uses_pickup_province() -> None:
    want = WantCriteria(provinces=["AB", "BC"])
    assert matches(_lot(), want, pickup_province="AB") is True
    assert matches(_lot(), want, pickup_province="SK") is False
    assert matches(_lot(), want, pickup_province=None) is False


def test_condition_floor_excludes_below_but_keeps_unknown() -> None:
    want = WantCriteria(condition_min="good")
    assert matches(_lot(condition_categorical="great"), want) is True
    assert matches(_lot(condition_categorical="good"), want) is True
    assert matches(_lot(condition_categorical="poor"), want) is False
    assert matches(_lot(condition_categorical=None), want) is True  # lenient


def test_showstoppers_hidden_by_default() -> None:
    flagged = _lot(showstopper_flags=[{"flag": "frame_rot", "evidence": "rust"}])
    assert matches(flagged, WantCriteria()) is False
    assert matches(flagged, WantCriteria(hide_showstoppers=False)) is True


def test_trim_is_lenient_on_unknown() -> None:
    want = WantCriteria(trims=["PRO-4X"])
    assert matches(_lot(trim="PRO-4X"), want) is True
    assert matches(_lot(trim=None), want) is True  # lenient on sparse data
    assert matches(_lot(trim="SE"), want) is False


def test_empty_string_in_lenient_field_is_treated_as_unknown() -> None:
    # An empty string is sparse/unknown data, not a known non-match.
    assert matches(_lot(trim=""), WantCriteria(trims=["PRO-4X"])) is True
    assert matches(_lot(transmission=""), WantCriteria(transmissions=["manual"])) is True


def test_province_match_strips_whitespace() -> None:
    want = WantCriteria(provinces=["AB"])
    assert matches(_lot(), want, pickup_province=" ab ") is True
    assert matches(_lot(), want, pickup_province="  ") is False


def test_sparse_inferred_condition_is_lenient_against_floor() -> None:
    # The enricher coerces low-confidence listings to "decent" and flags them
    # sparse; that "decent" is really unknown, so a good+ want must not drop it.
    want = WantCriteria(condition_min="good")
    sparse = _lot(condition_categorical="decent", condition_inferred_from_sparse_listing=True)
    assert matches(sparse, want) is True
    # A genuinely-confident "decent" is still correctly excluded by a good+ floor.
    confident = _lot(condition_categorical="decent", condition_inferred_from_sparse_listing=False)
    assert matches(confident, want) is False


def test_model_required_independently_of_make() -> None:
    # make matches; only the model differs → isolates the model predicate
    want = WantCriteria(makes=["Nissan"], models=["Xterra"])
    assert matches(_lot(make="Nissan", model="Frontier"), want) is False


def test_multiple_makes_models_any_one_matches() -> None:
    want = WantCriteria(makes=["Lexus", "Toyota"], models=["GX 470", "4Runner"])
    assert matches(_lot(make="Toyota", model="4Runner"), want) is True
    assert matches(_lot(make="Lexus", model="GX 470"), want) is True
    assert matches(_lot(make="Nissan", model="Xterra"), want) is False


def test_year_bounds_apply_independently() -> None:
    assert matches(_lot(year=2000), WantCriteria(year_min=2005)) is False
    assert matches(_lot(year=2010), WantCriteria(year_min=2005)) is True
    assert matches(_lot(year=2020), WantCriteria(year_max=2015)) is False
    assert matches(_lot(year=2010), WantCriteria(year_max=2015)) is True


def test_year_min_equals_max_is_an_exact_year() -> None:
    want = WantCriteria(year_min=2010, year_max=2010)
    assert matches(_lot(year=2010), want) is True
    assert matches(_lot(year=2011), want) is False


def test_condition_unknown_or_garbage_value_is_lenient() -> None:
    want = WantCriteria(condition_min="good")
    assert matches(_lot(condition_categorical="unknown"), want) is True
    assert matches(_lot(condition_categorical="weird"), want) is True


def test_mileage_cap_boundary_is_inclusive() -> None:
    assert matches(_lot(mileage_km=200_000), WantCriteria(max_mileage_km=200_000)) is True


def test_showstopper_excludes_even_when_all_else_matches() -> None:
    want = WantCriteria(makes=["Nissan"], models=["Xterra"], year_min=2005, year_max=2015)
    flagged = _lot(showstopper_flags=[{"flag": "frame_rot", "evidence": "rust"}])
    assert matches(flagged, want) is False


def test_model_spec_matches_within_its_year_range() -> None:
    crit = WantCriteria(model_specs=[
        ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009),
        ModelSpec(make="Lexus", model="GX 460", year_min=2010, year_max=2019),
    ])
    assert matches(_lot(make="Lexus", model="GX 470", year=2005), crit) is True
    assert matches(_lot(make="Lexus", model="GX 460", year=2015), crit) is True


def test_model_spec_excludes_sibling_out_of_its_range() -> None:
    crit = WantCriteria(model_specs=[
        ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009),
    ])
    # A GX 470 from 2015 is out of THIS spec's range → no match.
    assert matches(_lot(make="Lexus", model="GX 470", year=2015), crit) is False


def test_model_spec_trims_scoped_per_spec() -> None:
    crit = WantCriteria(model_specs=[
        ModelSpec(make="Toyota", model="4Runner", year_min=2003, year_max=2009, trims=["TRD"]),
    ])
    assert matches(_lot(make="Toyota", model="4Runner", year=2005, trim="TRD"), crit) is True
    assert matches(_lot(make="Toyota", model="4Runner", year=2005, trim="SR5"), crit) is False
    # Lenient on unknown trim (a buyer-assistant would rather over-alert).
    assert matches(_lot(make="Toyota", model="4Runner", year=2005, trim=None), crit) is True


def test_flat_path_unchanged_when_no_specs() -> None:
    # Regression: a legacy flat want must behave exactly as before.
    crit = WantCriteria(makes=["Nissan"], models=["Xterra"], year_min=2005, year_max=2015)
    assert matches(_lot(make="Nissan", model="Xterra", year=2010), crit) is True
    assert matches(_lot(make="Toyota", model="Tacoma", year=2010), crit) is False
