from __future__ import annotations

from decimal import Decimal
from typing import Any

from carbuyer.db.models import AuctionLot
from carbuyer.wants.criteria import WantCriteria
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
        "current_high_bid_cad": Decimal("8000"),
        "condition_categorical": "decent",
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
    assert matches(_lot(current_high_bid_cad=Decimal("9000")), want) is True
    assert matches(_lot(current_high_bid_cad=Decimal("12000")), want) is False
    assert matches(_lot(current_high_bid_cad=None), want) is True  # lenient


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
