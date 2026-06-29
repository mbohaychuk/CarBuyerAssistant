from __future__ import annotations

from decimal import Decimal

from carbuyer.scoring.score import (
    all_in_cost,
    cumulative_flag_weight,
    price_deal_score,
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


# ─── cumulative_flag_weight: authoritative weights, not the LLM blob ───


def test_cumulative_flag_weight_simple_sum() -> None:
    red = [{"flag": "rust_mentioned", "weight": -1}]
    green = [{"flag": "service_records", "weight": 2}]
    assert cumulative_flag_weight(red, green) == 1


def test_cumulative_flag_weight_ignores_hallucinated_llm_weight() -> None:
    """The LLM's weight field is advisory only; the authoritative weight comes
    from RED_FLAG_TAXONOMY. A hallucinated weight=-5 on a -1 flag must NOT
    cascade into the score (otherwise one hallucination could pull a lot past
    the excessive_red_flag_weight cutoff)."""
    # rust_mentioned is -1 in the taxonomy; the LLM here invented -5.
    red = [{"flag": "rust_mentioned", "weight": -5}]
    assert cumulative_flag_weight(red, []) == -1


def test_cumulative_flag_weight_unknown_flag_contributes_zero() -> None:
    """A flag name that doesn't appear in the taxonomy (typo, drift, future
    addition without taxonomy update) must NOT contribute its LLM weight."""
    red = [{"flag": "made_up_flag_name", "weight": -3}]
    assert cumulative_flag_weight(red, []) == 0


def test_cumulative_flag_weight_synthetic_vision_flag_carries_minus_two() -> None:
    """description_oversells_condition is intentionally outside the description
    taxonomy (Phase 8 overlay #18); its weight is pinned in
    _SYNTHETIC_FLAG_WEIGHTS so cumulative_flag_weight still recognizes it."""
    red = [{"flag": "description_oversells_condition", "evidence": "x", "weight": 999}]
    assert cumulative_flag_weight(red, []) == -2  # noqa: PLR2004
