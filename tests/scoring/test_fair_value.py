from __future__ import annotations

from decimal import Decimal

import pytest

from carbuyer.scoring.comps import ComparableSale
from carbuyer.scoring.fair_value import (
    HIGH_CONFIDENCE_MIN_COMPS,
    INSUFFICIENT_COMPS_THRESHOLD,
    ConfidenceBucket,
    compute_fair_value,
)


def _comp(
    price: int,
    *,
    channel: str = "auction_estate",
    mileage_km: int = 150000,
    year: int = 2015,
) -> ComparableSale:
    return ComparableSale(
        price_cad=Decimal(price), sale_channel=channel,
        year=year, mileage_km=mileage_km, days_listed=None,
        disposition_reason="sold", source="historical_sales",
    )


def test_compute_fair_value_ten_estate_comps_yields_high_confidence() -> None:
    comps = [_comp(p) for p in [
        10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000,
    ]]
    fv = compute_fair_value(comps, condition="decent")
    assert fv.value_low_cad is not None
    assert fv.value_mid_cad is not None
    assert fv.value_high_cad is not None
    assert fv.value_low_cad < fv.value_mid_cad < fv.value_high_cad
    # All channel-normalized to private (×1.20). decent is midpoint of
    # [p10, p90] which after normalization yields a value above the raw mid.
    assert fv.confidence == ConfidenceBucket.HIGH
    assert fv.comp_count == 10
    assert fv.expected_value_cad is not None


def test_compute_fair_value_below_threshold_returns_insufficient() -> None:
    comps = [_comp(10000)]
    fv = compute_fair_value(comps, condition="decent")
    assert fv.confidence == ConfidenceBucket.INSUFFICIENT
    assert fv.expected_value_cad is None
    assert fv.value_low_cad is None
    assert fv.value_mid_cad is None
    assert fv.value_high_cad is None
    assert fv.comp_count == 1


def test_compute_fair_value_five_comps_is_medium() -> None:
    comps = [_comp(p) for p in [10000, 11000, 12000, 13000, 14000]]
    fv = compute_fair_value(comps, condition="decent")
    assert fv.confidence == ConfidenceBucket.MEDIUM
    assert fv.expected_value_cad is not None


def test_compute_fair_value_condition_shifts_expected_value() -> None:
    comps = [_comp(p) for p in [
        10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000,
    ]]
    bad = compute_fair_value(comps, condition="bad")
    great = compute_fair_value(comps, condition="great")
    assert bad.expected_value_cad is not None
    assert great.expected_value_cad is not None
    # great → p90 region; bad → p10 region.
    assert great.expected_value_cad > bad.expected_value_cad


def test_compute_fair_value_sparse_decent_below_confident_decent() -> None:
    """Phase 4 overlay #9: sparse-listing-coerced decent must price below
    confident decent so the dual write of condition_inferred_from_sparse_listing
    is not dead."""
    comps = [_comp(p) for p in [
        10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000,
    ]]
    confident = compute_fair_value(comps, condition="decent", sparse=False)
    sparse = compute_fair_value(comps, condition="decent", sparse=True)
    assert confident.expected_value_cad is not None
    assert sparse.expected_value_cad is not None
    assert sparse.expected_value_cad < confident.expected_value_cad


def test_compute_fair_value_normalizes_channel_mix() -> None:
    # Mix of dealer (×0.92) and estate (×1.20) — the resulting range should
    # span both normalized populations.
    comps = (
        [_comp(p, channel="auction_estate") for p in [12000, 13000, 14000, 15000, 16000]]
        + [_comp(p, channel="dealer") for p in [16000, 17000, 18000, 19000, 20000]]
    )
    fv = compute_fair_value(comps, condition="decent")
    assert fv.value_low_cad is not None and fv.value_high_cad is not None
    # Estate normalized: 14400..19200; Dealer normalized: 14720..18400.
    # Combined p10 should be near the bottom of estate, p90 near top of estate.
    assert fv.value_low_cad < Decimal("16000")
    assert fv.value_high_cad > Decimal("18000")


def test_compute_fair_value_trims_mileage_outliers() -> None:
    # 9 normal comps + 1 huge mileage outlier (>2 sd). Outlier must be
    # trimmed; trimmed result still has 9 comps which is medium confidence.
    normal = [_comp(p, mileage_km=150000) for p in [
        10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000,
    ]]
    outlier = _comp(5000, mileage_km=900000)
    fv = compute_fair_value([*normal, outlier], condition="decent")
    assert fv.comp_count == 9
    assert fv.confidence == ConfidenceBucket.MEDIUM
    # Outlier (5000) excluded → expected value above 5000.
    assert fv.expected_value_cad is not None
    assert fv.expected_value_cad > Decimal("5000")


def test_thresholds_have_sane_relationship() -> None:
    assert INSUFFICIENT_COMPS_THRESHOLD < HIGH_CONFIDENCE_MIN_COMPS
