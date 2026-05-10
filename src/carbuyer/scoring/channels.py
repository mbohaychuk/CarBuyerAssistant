"""Sale-channel normalization + condition mapping.

The auction MVP collects historical sale prices from heterogeneous channels
(private, dealer, multiple auction subtypes). Each channel sets its own price
floor relative to a private-party retail comp:

- ``auction_estate``: estate / closeout liquidations clear well below private
  retail because the timeline is fixed. We multiply observed final-bid up to
  approximate "what a private buyer would pay."
- ``dealer``: asking prices include a retail markup; we back it off.
- ``auction_salvage``: a fraction of any retail comp; here for completeness
  but unused in MVP comp-set construction.

The condition→position table maps the LLM's categorical condition rating to
a [0, 1] position between the comp set's p10 and p90. Phase 4 overlay #8
shifts a sparse-listing-coerced ``decent`` toward p25 because the enricher
flagged it as a "we don't know" rather than a confident assessment.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Final


CHANNEL_MULTIPLIERS: Final[dict[str, Decimal]] = {
    "private": Decimal("1.00"),
    "dealer": Decimal("0.92"),
    "auction_estate": Decimal("1.20"),
    "auction_govt": Decimal("1.15"),
    "auction_commercial": Decimal("1.10"),
    # Salvage comps are not used in the MVP comp set; documented here so the
    # multiplier table is complete and consistent.
    "auction_salvage": Decimal("0.50"),
}

CONDITION_POSITION: Final[dict[str, float]] = {
    "bad": 0.0,
    "poor": 0.25,
    "decent": 0.50,
    "good": 0.75,
    "great": 1.0,
}

# Phase 4 overlay #8: position used when the enricher coerced
# condition_categorical='decent' under low confidence. p25-ish — sparse
# listings historically run worse than honestly-decent comps.
SPARSE_DECENT_POSITION: Final[float] = 0.35


def normalize_to_private(price_cad: Decimal, channel: str) -> Decimal:
    """Convert a price from ``channel`` to its private-party equivalent.

    Unknown channel falls back to identity (1.00) so a stray sale_channel
    string doesn't poison the comp set; the row still contributes its raw
    price as a private-equivalent.
    """
    return price_cad * CHANNEL_MULTIPLIERS.get(channel, Decimal("1.00"))


def condition_position(condition: str, *, sparse: bool = False) -> float:
    """Map a categorical condition to a [0, 1] position between p10 and p90.

    Phase 4 overlay #8: when ``sparse=True`` the enricher coerced
    ``condition_categorical='decent'`` because ``condition_confidence < 0.5``
    (see ``carbuyer.apps.enricher.enricher._apply_to_lot``). That value
    reflects "we don't know" not "actually decent" — shift toward p25 so
    sparse listings price below confidently-decent comps. The sparse flag is
    only honored on ``decent``; other categorical values come with their own
    confidence and are not coerced by the enricher.
    """
    if sparse and condition == "decent":
        return SPARSE_DECENT_POSITION
    return CONDITION_POSITION.get(condition, 0.5)
