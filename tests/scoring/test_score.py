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


def test_all_in_cost_caps_premium_when_max_active() -> None:
    # McDougall regime: 15% would be 3000 on a 20k bid; cap pins it at 2000.
    cost = all_in_cost(
        current_high_bid=Decimal("20000"),
        buyer_premium_pct=Decimal("0.15"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("0"),
        buyer_premium_max_cad=Decimal("2000"),
        buyer_premium_min_cad=Decimal("20"),
    )
    # (20000 + 2000) * 1.05 = 23100.
    assert cost == Decimal("23100.00")


def test_all_in_cost_floors_premium_when_min_active() -> None:
    # 15% of 100 = 15; floor pins it at 20.
    cost = all_in_cost(
        current_high_bid=Decimal("100"),
        buyer_premium_pct=Decimal("0.15"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("0"),
        buyer_premium_max_cad=Decimal("2000"),
        buyer_premium_min_cad=Decimal("20"),
    )
    # (100 + 20) * 1.05 = 126.
    assert cost == Decimal("126.00")


def test_all_in_cost_linear_when_inside_cap_and_floor() -> None:
    # 15% of 10000 = 1500; comfortably between 20 floor and 2000 cap.
    cost = all_in_cost(
        current_high_bid=Decimal("10000"),
        buyer_premium_pct=Decimal("0.15"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("0"),
        buyer_premium_max_cad=Decimal("2000"),
        buyer_premium_min_cad=Decimal("20"),
    )
    # (10000 + 1500) * 1.05 = 12075.
    assert cost == Decimal("12075.00")


def test_all_in_cost_at_cap_boundary_uses_linear() -> None:
    # Bid exactly hits the cap-boundary: 13333.33... * 0.15 = 2000 exact.
    # The clamp's `>` means equality stays linear; results match either way.
    bid = Decimal("13333.33")
    cost_capped = all_in_cost(
        current_high_bid=bid, buyer_premium_pct=Decimal("0.15"),
        gst_pct=Decimal("0"), pst_pct=Decimal("0"),
        landed_cost_premium=Decimal("0"),
        buyer_premium_max_cad=Decimal("2000"),
    )
    cost_linear = all_in_cost(
        current_high_bid=bid, buyer_premium_pct=Decimal("0.15"),
        gst_pct=Decimal("0"), pst_pct=Decimal("0"),
        landed_cost_premium=Decimal("0"),
    )
    # 13333.33 * 0.15 = 1999.9995 → just under 2000 cap → linear path.
    assert cost_capped == cost_linear


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
    assert 0.32 < s < 0.34  # noqa: PLR2004


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
    assert s == 3.0  # noqa: PLR2004


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
    assert s == 5.0  # noqa: PLR2004


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
    assert 15140 < float(bid) < 15165  # noqa: PLR2004


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


def test_recommended_max_bid_in_capped_regime_pins_premium() -> None:
    # High-value lot: linear answer would imply a premium past the $2000 cap.
    # Expected behaviour: solver picks the capped-regime bid where premium=cap.
    bid = recommended_max_bid(
        expected_value=Decimal("50000"),
        buyer_premium_pct=Decimal("0.15"),
        gst_pct=Decimal("0"),
        pst_pct=Decimal("0"),
        landed_cost_premium=Decimal("0"),
        flip_margin=Decimal("2000"),
        buyer_premium_max_cad=Decimal("2000"),
        buyer_premium_min_cad=Decimal("20"),
    )
    # target_all_in = 48000; bid+premium=48000; cap pins premium=2000; bid=46000.
    # Sanity: 46000 * 0.15 = 6900 > 2000 cap, so cap regime is correct.
    assert bid == Decimal("46000")


def test_recommended_max_bid_in_floored_regime_pins_premium() -> None:
    # Very low-value lot: linear premium would be below the $20 floor.
    bid = recommended_max_bid(
        expected_value=Decimal("100"),
        buyer_premium_pct=Decimal("0.15"),
        gst_pct=Decimal("0"),
        pst_pct=Decimal("0"),
        landed_cost_premium=Decimal("0"),
        flip_margin=Decimal("0"),
        buyer_premium_max_cad=Decimal("2000"),
        buyer_premium_min_cad=Decimal("20"),
    )
    # target_all_in = 100; bid+premium=100; floor pins premium=20; bid=80.
    # Sanity: 80 * 0.15 = 12 < 20 floor, so floor regime is correct.
    assert bid == Decimal("80")


def test_recommended_max_bid_in_linear_regime_with_bounds_present() -> None:
    # Mid-range bid: linear premium stays between floor and cap → linear answer.
    # Identical to the no-cap/no-floor test, confirming the bounds don't
    # perturb the answer when they aren't binding.
    bid_with_bounds = recommended_max_bid(
        expected_value=Decimal("20000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("500"),
        flip_margin=Decimal("2000"),
        buyer_premium_max_cad=Decimal("2000"),
        buyer_premium_min_cad=Decimal("20"),
    )
    bid_no_bounds = recommended_max_bid(
        expected_value=Decimal("20000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("500"),
        flip_margin=Decimal("2000"),
    )
    assert bid_with_bounds == bid_no_bounds


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
    assert flag_score(red, []) == -5  # noqa: PLR2004


def test_flag_score_clipped_to_plus_five() -> None:
    green = [{"flag": "no_accidents_carfax", "weight": 2}] * 4  # +8 → clip 5
    assert flag_score([], green) == 5  # noqa: PLR2004


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
    assert flag_score([], green, description_quality="thin") == 2  # noqa: PLR2004


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
    assert flag_score(red, []) == -3  # noqa: PLR2004


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
    assert flag_score(red, []) == -5  # noqa: PLR2004


def test_flag_score_dilution_cap_with_offsetting_greens() -> None:
    # 5 light reds (cap -2) + 3 light greens (+3) → +1.
    red = [
        {"flag": name, "weight": -1}
        for name in (
            "out_of_province", "winter_tires_only", "mileage_unknown",
            "no_service_records", "smoker_owned",
        )
    ]
    green = [
        {"flag": name, "weight": 1}
        for name in ("single_owner", "garage_kept", "non_smoker")
    ]
    assert flag_score(red, green) == 1


def test_flag_score_constants_are_consistent() -> None:
    assert LIGHT_RED_DILUTION_THRESHOLD == 3  # noqa: PLR2004
    assert LIGHT_RED_DILUTION_CAP == -2  # noqa: PLR2004
    assert THIN_DESCRIPTION_FLAG_FLOOR == -2  # noqa: PLR2004


def test_flag_score_ignores_hallucinated_llm_weight() -> None:
    """The LLM's weight field is advisory only; the authoritative weight comes
    from RED_FLAG_TAXONOMY. A hallucinated weight=-5 on a -1 flag must NOT
    cascade into the score (otherwise one hallucination could pull a lot past
    the excessive_red_flag_weight cutoff)."""
    # rust_mentioned is -1 in the taxonomy; the LLM here invented -5.
    red = [{"flag": "rust_mentioned", "weight": -5}]
    assert flag_score(red, []) == -1


def test_flag_score_unknown_flag_contributes_zero() -> None:
    """A flag name that doesn't appear in the taxonomy (typo, drift, future
    addition without taxonomy update) must NOT contribute its LLM weight."""
    red = [{"flag": "made_up_flag_name", "weight": -3}]
    assert flag_score(red, []) == 0


def test_flag_score_synthetic_vision_flag_carries_minus_two() -> None:
    """description_oversells_condition is intentionally outside the description
    taxonomy (Phase 8 overlay #18); its weight is pinned in
    _SYNTHETIC_FLAG_WEIGHTS so flag_score still recognizes it."""
    red = [{"flag": "description_oversells_condition", "evidence": "x", "weight": 999}]
    assert flag_score(red, []) == -2  # noqa: PLR2004


def test_flag_score_unaffected_by_concerns() -> None:
    """Advisory `concerns` are explicitly an advisory channel — they never
    move rankings. flag_score takes only taxonomy flags; a lot carrying
    populated llm_concerns must score identically to one without. Locking
    that design decision so a future refactor can't quietly fold concerns
    into the score."""
    red = [{"flag": "rust_mentioned", "weight": -1}]
    green = [{"flag": "service_records", "weight": 2}]
    severe_concerns = [
        {"text": "blue smoke on cold start → worn valve seals", "severity": "moderate"},
        {"text": "seller is a dealer, not the owner", "severity": "minor"},
        {"text": "no test-drive offered", "severity": "moderate"},
    ]

    without_concerns = flag_score(red, green)
    # flag_score has no `concerns` parameter — concerns cannot reach it. The
    # only inputs are red, green, and description_quality, so an
    # otherwise-identical call yields the identical score regardless of how
    # many concerns the enricher attached to the lot row.
    with_concerns = flag_score(red, green)
    assert with_concerns == without_concerns
    assert "concerns" not in flag_score.__code__.co_varnames
    assert severe_concerns
