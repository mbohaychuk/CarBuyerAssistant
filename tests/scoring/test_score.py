from __future__ import annotations

from decimal import Decimal

from carbuyer.scoring.score import (
    LIGHT_RED_DILUTION_CAP,
    LIGHT_RED_DILUTION_THRESHOLD,
    THIN_DESCRIPTION_FLAG_FLOOR,
    RarityInputs,
    all_in_cost,
    flag_score,
    price_deal_score,
    rarity_score,
    recommended_max_bid,
)


# ─── all_in_cost ───


def test_all_in_cost_applies_bp_then_tax_then_landed() -> None:
    cost = all_in_cost(
        current_high_bid=Decimal("10000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("500"),
    )
    # 10000 * 1.10 * 1.05 = 11550; + 500 = 12050.
    assert cost == Decimal("12050.00")


# ─── price_deal_score ───


def test_price_deal_score_positive_when_underpriced() -> None:
    s = price_deal_score(
        current_high_bid=Decimal("10000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("500"),
        expected_value=Decimal("18000"),
    )
    # all_in = 12050; (18000 - 12050) / 18000 ≈ 0.3306
    assert 0.32 < s < 0.34


def test_price_deal_score_negative_when_overpriced() -> None:
    s = price_deal_score(
        current_high_bid=Decimal("20000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("500"),
        expected_value=Decimal("18000"),
    )
    assert s < 0


def test_price_deal_score_zero_expected_value_returns_zero() -> None:
    # Defensive: insufficient comps shouldn't propagate as a div-by-zero.
    assert price_deal_score(
        current_high_bid=Decimal("1000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("0"),
        expected_value=Decimal("0"),
    ) == 0.0


# ─── rarity_score ───


def test_rarity_low_comp_count_with_desirable_yields_three() -> None:
    s = rarity_score(RarityInputs(
        desirable_trim_or_spec=True, classic_or_collector=False,
        historical_comp_count=1, recent_appreciation=None,
    ))
    # 2.0 (low+desirable) + 1.0 (desirable_trim) = 3.0
    assert s == 3.0


def test_rarity_low_comp_count_undesirable_yields_zero() -> None:
    s = rarity_score(RarityInputs(
        desirable_trim_or_spec=False, classic_or_collector=False,
        historical_comp_count=1, recent_appreciation=None,
    ))
    assert s == 0.0


def test_rarity_classic_with_appreciation_caps_at_five() -> None:
    s = rarity_score(RarityInputs(
        desirable_trim_or_spec=True, classic_or_collector=True,
        historical_comp_count=1, recent_appreciation=0.20,
    ))
    # 2.0 (low+desirable) + 1.5 (classic) + 1.0 (desirable_trim) + 1.0 (appreciation) = 5.5 → cap 5
    assert s == 5.0


def test_rarity_high_comp_count_no_low_bonus() -> None:
    s = rarity_score(RarityInputs(
        desirable_trim_or_spec=True, classic_or_collector=False,
        historical_comp_count=50, recent_appreciation=None,
    ))
    # No "low+desirable" bonus when comps abundant; just desirable_trim.
    assert s == 1.0


# ─── recommended_max_bid ───


def test_recommended_max_bid_backs_out_margin() -> None:
    bid = recommended_max_bid(
        expected_value=Decimal("20000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("500"),
        flip_margin=Decimal("2000"),
    )
    # target_all_in = 18000; (18000 - 500) / (1.10 * 1.05) = 17500 / 1.155
    assert bid is not None
    assert 15140 < float(bid) < 15165


def test_recommended_max_bid_returns_none_when_margin_exceeds_value() -> None:
    bid = recommended_max_bid(
        expected_value=Decimal("1500"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("0"),
        flip_margin=Decimal("2000"),
    )
    assert bid is None


# ─── flag_score: overlay #10 (thin floor) + overlay #11 (dilution cap) ───


def test_flag_score_simple_sum_when_within_bounds() -> None:
    red = [{"flag": "rust_mentioned", "weight": -1}]
    green = [{"flag": "service_records", "weight": 2}]
    assert flag_score(red, green) == 1


def test_flag_score_clipped_to_minus_five() -> None:
    # Two heavy reds total -8 → clip to -5.
    red = [
        {"flag": "engine_knock", "weight": -3},
        {"flag": "transmission_slipping", "weight": -3},
        {"flag": "frame_rust", "weight": -3},
    ]
    assert flag_score(red, []) == -5


def test_flag_score_clipped_to_plus_five() -> None:
    green = [{"flag": "no_accidents_carfax", "weight": 2}] * 4  # +8 → clip 5
    assert flag_score([], green) == 5


def test_flag_score_thin_description_caps_floor_at_minus_two() -> None:
    # Phase 4 overlay #10: thin descriptions can't surface enough evidence
    # to legitimately score below -2; the heavy weights here are noise.
    red = [
        {"flag": "engine_knock", "weight": -3},
        {"flag": "transmission_slipping", "weight": -3},
    ]
    assert flag_score(red, [], description_quality="thin") == THIN_DESCRIPTION_FLAG_FLOOR


def test_flag_score_thin_description_does_not_clip_positive() -> None:
    green = [{"flag": "no_accidents_carfax", "weight": 2}]
    assert flag_score([], green, description_quality="thin") == 2


def test_flag_score_dilution_cap_when_many_light_reds() -> None:
    # Phase 4 overlay #11: 5 magnitude-1 reds is "context flag dilution",
    # not a -5 verdict. Cap their contribution at -2.
    red = [
        {"flag": "out_of_province", "weight": -1},
        {"flag": "winter_tires_only", "weight": -1},
        {"flag": "mileage_unknown", "weight": -1},
        {"flag": "no_service_records", "weight": -1},
        {"flag": "smoker_owned", "weight": -1},
    ]
    # >3 mag-1 reds → cap at -2 (not -5 / clip).
    assert flag_score(red, []) == LIGHT_RED_DILUTION_CAP


def test_flag_score_dilution_cap_does_not_apply_at_threshold() -> None:
    # Exactly 3 mag-1 reds is on the "context isn't dominating" side; no cap.
    red = [
        {"flag": "out_of_province", "weight": -1},
        {"flag": "winter_tires_only", "weight": -1},
        {"flag": "mileage_unknown", "weight": -1},
    ]
    assert flag_score(red, []) == -3


def test_flag_score_heavy_reds_still_count_with_dilution_cap() -> None:
    # 5 light reds (capped at -2) plus a heavy -3 red → -5 (clipped).
    red = [
        {"flag": "out_of_province", "weight": -1},
        {"flag": "winter_tires_only", "weight": -1},
        {"flag": "mileage_unknown", "weight": -1},
        {"flag": "no_service_records", "weight": -1},
        {"flag": "smoker_owned", "weight": -1},
        {"flag": "engine_knock", "weight": -3},
    ]
    # light_red_sum capped at -2; heavy = -3; sum = -5; clip -5 floor.
    assert flag_score(red, []) == -5


def test_flag_score_dilution_cap_with_offsetting_greens() -> None:
    # 5 light reds (cap -2) + 3 light greens (+3) → +1.
    red = [{"flag": f"light_{i}", "weight": -1} for i in range(5)]
    green = [{"flag": f"good_{i}", "weight": 1} for i in range(3)]
    assert flag_score(red, green) == 1


def test_flag_score_constants_are_consistent() -> None:
    assert LIGHT_RED_DILUTION_THRESHOLD == 3
    assert LIGHT_RED_DILUTION_CAP == -2
    assert THIN_DESCRIPTION_FLAG_FLOOR == -2
