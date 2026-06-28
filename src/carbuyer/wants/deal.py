"""Want-relative deal quality for a matched lot — the "is this a good buy, and why".

A thin layer over the valuation the existing pipeline already computes (scoring/):
it reuses the lot's expected_value / value_mid and compares the listing's actual
price against it. `score` (fraction below the market reference) is the rankable
number stored on want_matches.want_relative_score; the rest of WantDeal is the
"why" an alert shows (dollars below market, dollars under the want's ceiling, comp
count for confidence).

Price is injected (offer_price_cad) for the same reason the matcher injects it: it
is channel-specific (auction high bid vs private asking price) while the valuation
fields live on the source-agnostic lot/offer.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from carbuyer.db.models import VehicleOffer
from carbuyer.wants.criteria import WantCriteria


@dataclass(frozen=True)
class WantDeal:
    score: float | None  # fraction below the market reference; None if not computable
    reference_value_cad: Decimal | None  # expected_value, or value_mid as fallback
    dollars_below_market_cad: Decimal | None
    dollars_under_ceiling_cad: Decimal | None
    comp_count: int | None


def score_want_deal(
    lot: VehicleOffer,
    criteria: WantCriteria,
    *,
    offer_price_cad: Decimal | int | None,
) -> WantDeal:
    reference = lot.expected_value_cad or lot.value_mid_cad

    score: float | None = None
    dollars_below: Decimal | None = None
    if reference is not None and reference > 0 and offer_price_cad is not None:
        dollars_below = reference - offer_price_cad
        score = float(dollars_below / reference)

    dollars_under_ceiling: Decimal | None = None
    if criteria.price_ceiling_cad is not None and offer_price_cad is not None:
        dollars_under_ceiling = Decimal(criteria.price_ceiling_cad) - offer_price_cad

    return WantDeal(
        score=score,
        reference_value_cad=reference,
        dollars_below_market_cad=dollars_below,
        dollars_under_ceiling_cad=dollars_under_ceiling,
        comp_count=lot.comp_count,
    )
