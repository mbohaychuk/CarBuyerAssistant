"""Scoring functions: deal score, rarity, recommended bid, flag score.

These are pure-CPU helpers consumed by the valuator worker. Each function
takes its inputs explicitly so tests don't need DB / settings access.

Phase 4 overlay #10 + #11: ``flag_score`` is the only function with
nontrivial behavior beyond the plan — it caps dilution from many low-magnitude
"context" red flags (mileage_unknown, out_of_province, etc.) and tightens the
floor for low-evidence (thin) listings. See the docstring on ``flag_score``.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# Phase 4 overlay #11 constants. When more than this many magnitude-1 RED
# flags fire, their cumulative contribution is capped at LIGHT_RED_DILUTION_CAP
# so a typical "out_of_province + winter_tires_only + mileage_unknown +
# no_service_records" RB listing doesn't read as -4 before any actual issue.
LIGHT_RED_DILUTION_THRESHOLD = 3
LIGHT_RED_DILUTION_CAP = -2

# Phase 4 overlay #10: a thin description literally cannot surface enough
# evidence to legitimately score below -2 — the weights that fired likely
# reflect listing-sparsity friction (mileage_unknown, no_service_records)
# rather than vehicle-quality issues. Confident verbose listings keep -5.
THIN_DESCRIPTION_FLAG_FLOOR = -2

FLAG_SCORE_DEFAULT_FLOOR = -5
FLAG_SCORE_CEILING = 5


@dataclass(slots=True)
class RarityInputs:
    desirable_trim_or_spec: bool
    classic_or_collector: bool
    historical_comp_count: int
    recent_appreciation: float | None


def all_in_cost(
    *,
    current_high_bid: Decimal,
    buyer_premium_pct: Decimal,
    gst_pct: Decimal,
    pst_pct: Decimal,
    landed_cost_premium: Decimal,
) -> Decimal:
    """bid × (1+BP) × (1+GST+PST) + landed cost. Caller controls all rates."""
    bp_factor = Decimal("1") + buyer_premium_pct
    tax_factor = Decimal("1") + gst_pct + pst_pct
    bid_with_premium = current_high_bid * bp_factor * tax_factor
    return bid_with_premium + landed_cost_premium


def price_deal_score(
    *,
    current_high_bid: Decimal,
    buyer_premium_pct: Decimal,
    gst_pct: Decimal,
    pst_pct: Decimal,
    landed_cost_premium: Decimal,
    expected_value: Decimal,
) -> float:
    """(expected_value - all_in) / expected_value. Returns 0.0 when the
    expected value is non-positive — that means insufficient comps and we
    should not pretend to score the deal."""
    if expected_value <= 0:
        return 0.0
    total = all_in_cost(
        current_high_bid=current_high_bid,
        buyer_premium_pct=buyer_premium_pct,
        gst_pct=gst_pct,
        pst_pct=pst_pct,
        landed_cost_premium=landed_cost_premium,
    )
    return float((expected_value - total) / expected_value)


def rarity_score(inputs: RarityInputs) -> float:
    """Bounded [0, 5]. Flat additive components — easy to inspect, easy to
    rebalance. Low comp count + desirability is the dominant term because
    that's the situation where the comp-set signal is weakest."""
    score = 0.0
    low_comp_with_desirability = (
        inputs.historical_comp_count < 3
        and (inputs.desirable_trim_or_spec or inputs.classic_or_collector)
    )
    if low_comp_with_desirability:
        score += 2.0
    if inputs.classic_or_collector:
        score += 1.5
    if inputs.desirable_trim_or_spec:
        score += 1.0
    if inputs.recent_appreciation is not None and inputs.recent_appreciation > 0.05:
        score += 1.0
    return min(score, 5.0)


def recommended_max_bid(
    *,
    expected_value: Decimal,
    buyer_premium_pct: Decimal,
    gst_pct: Decimal,
    pst_pct: Decimal,
    landed_cost_premium: Decimal,
    flip_margin: Decimal,
) -> Decimal | None:
    """Inverse of all_in_cost — the bid that would land the lot at
    ``expected_value - flip_margin``. Returns None when the math goes
    non-positive (margin >= value, or BP/tax/landed eat the whole thing)."""
    target_all_in = expected_value - flip_margin
    if target_all_in <= 0:
        return None
    bp_tax = (Decimal("1") + buyer_premium_pct) * (Decimal("1") + gst_pct + pst_pct)
    bid = (target_all_in - landed_cost_premium) / bp_tax
    return bid if bid > 0 else None


def _weight(f: dict[str, Any]) -> int:
    return int(f.get("weight", 0))


def _sum_weights(flags: Iterable[dict[str, Any]]) -> int:
    return sum(_weight(f) for f in flags)


def flag_score(
    red: list[dict[str, Any]],
    green: list[dict[str, Any]],
    *,
    description_quality: str | None = None,
) -> int:
    """Cumulative flag weight, with two domain corrections, clipped to [-5, 5].

    Phase 4 overlay #11 (context flag dilution cap):
      RB / industrial-yard listings reliably fire 4+ magnitude-1 red flags
      (out_of_province + winter_tires_only + mileage_unknown +
      no_service_records + smoker_owned) before any actual issue surfaces.
      That would sum to -4..-5 and dominate scoring of every Western-Canada
      auction-yard lot. So: when more than LIGHT_RED_DILUTION_THRESHOLD
      magnitude-1 RED flags fire, cap their cumulative contribution at
      LIGHT_RED_DILUTION_CAP. Heavy reds (|w| >= 2) and all greens add
      normally on top.

    Phase 4 overlay #10 (thin-description floor):
      A "thin" description literally cannot surface enough evidence to
      legitimately score below THIN_DESCRIPTION_FLAG_FLOOR — the flags that
      fired likely reflect listing-sparsity friction. Floor at -2 in that case;
      confident verbose listings keep the full -5 floor.
    """
    light_red = [f for f in red if abs(_weight(f)) == 1]
    heavy_red = [f for f in red if abs(_weight(f)) != 1]

    light_red_sum = _sum_weights(light_red)
    if len(light_red) > LIGHT_RED_DILUTION_THRESHOLD:
        light_red_sum = max(LIGHT_RED_DILUTION_CAP, light_red_sum)

    total = light_red_sum + _sum_weights(heavy_red) + _sum_weights(green)

    floor = (
        THIN_DESCRIPTION_FLAG_FLOOR
        if description_quality == "thin"
        else FLAG_SCORE_DEFAULT_FLOOR
    )
    return max(floor, min(FLAG_SCORE_CEILING, total))


def cumulative_flag_weight(
    red: list[dict[str, Any]],
    green: list[dict[str, Any]],
) -> int:
    """Raw pre-clip / pre-dilution-cap sum of all flag weights.

    Phase 4 overlay #12 uses this to skip notification on lots whose total
    red weight is at or below ``settings.excessive_red_flag_weight_threshold``,
    even when ``flag_score`` would have clipped to -5 and made them look the
    same as merely-bad lots.
    """
    return _sum_weights(red) + _sum_weights(green)
