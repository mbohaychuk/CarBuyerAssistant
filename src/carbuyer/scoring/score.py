"""Scoring functions: all-in cost, deal score, flag weight.

These are pure-CPU helpers consumed by the valuator worker. Each function
takes its inputs explicitly so tests don't need DB / settings access.

The flag-weight lookup (``FLAG_WEIGHT_LOOKUP``) is authoritative: the enricher's
prompt instructs the LLM to copy weights verbatim, but a hallucinated weight
would silently move a lot past the ``excessive_red_flag_weight_threshold``
notification cutoff, so weights are looked up here at score time and the LLM's
``weight`` field in the JSON blob is ignored.
"""
from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from carbuyer.flags.taxonomy import GREEN_FLAG_TAXONOMY, RED_FLAG_TAXONOMY
from carbuyer.shared.logging import get_logger

_log = get_logger("scoring.score")

# Authoritative flag→weight map built from the taxonomy at module load. The
# enricher's prompt instructs the LLM to copy weights verbatim, but a
# hallucinated -5 on a -1 flag (or any drift between schema and prompt) would
# silently pull lots past the excessive_red_flag_weight cutoff. Look up at
# score time and ignore whatever weight the LLM put in the JSON blob.
#
# `description_oversells_condition` is a vision-pass synthetic flag that
# intentionally lives outside the description taxonomy (Phase 8 overlay #18);
# its -2 weight is pinned here so cumulative_flag_weight still scores it.
_SYNTHETIC_FLAG_WEIGHTS: dict[str, int] = {
    "description_oversells_condition": -2,
}
FLAG_WEIGHT_LOOKUP: dict[str, int] = {
    **{f["flag"]: f["weight"] for f in RED_FLAG_TAXONOMY},
    **{f["flag"]: f["weight"] for f in GREEN_FLAG_TAXONOMY},
    **_SYNTHETIC_FLAG_WEIGHTS,
}


def _premium_amount(
    bid: Decimal,
    pct: Decimal,
    cap: Decimal | None,
    floor: Decimal | None,
) -> Decimal:
    """Linear premium clamped against optional cap/floor. Both None gives the
    prior unconstrained behaviour."""
    premium = bid * pct
    if cap is not None and premium > cap:
        premium = cap
    if floor is not None and premium < floor:
        premium = floor
    return premium


def all_in_cost(
    *,
    current_high_bid: Decimal,
    buyer_premium_pct: Decimal,
    gst_pct: Decimal,
    pst_pct: Decimal,
    landed_cost_premium: Decimal,
    buyer_premium_max_cad: Decimal | None = None,
    buyer_premium_min_cad: Decimal | None = None,
) -> Decimal:
    """(bid + clamp(bid*BP, min, max)) * (1+GST+PST) + landed.

    With both cap/floor None this collapses to ``bid * (1+BP) * (1+GST+PST) +
    landed`` — the prior formula. McDougall ("15% to Max $2000 Min $20") sets
    both; HiBid leaves both None.
    """
    premium = _premium_amount(
        current_high_bid, buyer_premium_pct,
        buyer_premium_max_cad, buyer_premium_min_cad,
    )
    tax_factor = Decimal("1") + gst_pct + pst_pct
    return (current_high_bid + premium) * tax_factor + landed_cost_premium


def price_deal_score(
    *,
    current_high_bid: Decimal,
    buyer_premium_pct: Decimal,
    gst_pct: Decimal,
    pst_pct: Decimal,
    landed_cost_premium: Decimal,
    expected_value: Decimal,
    buyer_premium_max_cad: Decimal | None = None,
    buyer_premium_min_cad: Decimal | None = None,
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
        buyer_premium_max_cad=buyer_premium_max_cad,
        buyer_premium_min_cad=buyer_premium_min_cad,
    )
    return float((expected_value - total) / expected_value)


def _weight(f: dict[str, Any]) -> int:
    """Authoritative weight from the taxonomy, not from the LLM blob.

    Unknown flag names fall back to 0 with a log line so a typo or drift
    surfaces without taking the score down. The LLM's `f["weight"]` field is
    ignored — see FLAG_WEIGHT_LOOKUP module docstring.
    """
    flag_name = f.get("flag", "")
    if flag_name in FLAG_WEIGHT_LOOKUP:
        return FLAG_WEIGHT_LOOKUP[flag_name]
    _log.warning(
        "unknown flag at scoring time; weight=0",
        flag=flag_name,
        llm_weight=f.get("weight"),
    )
    return 0


def _sum_weights(flags: Iterable[dict[str, Any]]) -> int:
    return sum(_weight(f) for f in flags)


def cumulative_flag_weight(
    red: list[dict[str, Any]],
    green: list[dict[str, Any]],
) -> int:
    """Raw sum of all flag weights.

    Phase 4 overlay #12 uses this to skip notification on lots whose total
    red weight is at or below ``settings.excessive_red_flag_weight_threshold``.
    """
    return _sum_weights(red) + _sum_weights(green)
