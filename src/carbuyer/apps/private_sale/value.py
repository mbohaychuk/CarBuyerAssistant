"""Valuation for private listings — reuses the pure ``scoring/`` library.

Call sequence mirrors ``apps/valuator/valuator.value_one``:
  1. Guard: if make/model/year missing → INSUFFICIENT, return.
  2. ``build_comp_set`` → comps.
  3. ``compute_fair_value`` → FairValue (expected_value, confidence_bucket).
  4. ``_historical_comp_count`` → count for rarity inputs.
  5. ``rarity_score(RarityInputs(...))`` → listing.rarity_score.
  6. ``flag_score(red, green, description_quality=None)`` → listing.flag_score.
     (PrivateListing has no description_quality column; pass None.)
  7. ``landed_cost_premium`` using ask_price_cad as the "bid".
  8. ``all_in_cost`` / ``price_deal_score`` with buyer_premium_pct=0,
     gst_pct=0, pst_pct=0 (private sale — no buyer premium or taxes).
  9. Set valuation_status=DONE (or INSUFFICIENT when confidence too low).

Private-sale pricing: the ask IS the price, so current_high_bid=ask_price_cad.
No buyer premium, no tax. ``landed_cost_premium`` uses the real provincial
distance — same as the auction valuator, but dest defaults to home_province when
pickup_province is None.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import ValuationStatus
from carbuyer.db.models import HistoricalSale, PrivateListing
from carbuyer.scoring.comps import build_comp_set
from carbuyer.scoring.fair_value import ConfidenceBucket, compute_fair_value
from carbuyer.scoring.landed_cost import distance_km_between, landed_cost_premium
from carbuyer.scoring.score import (
    RarityInputs,
    all_in_cost,
    flag_score,
    price_deal_score,
    rarity_score,
)
from carbuyer.shared.config import settings

_ZERO = Decimal("0")


async def _historical_comp_count(
    session: AsyncSession, *, make: str, model: str,
) -> int:
    """Broader-than-comp-set count for rarity scoring: same make+model
    regardless of year window or trim. Mirrors the auction valuator's helper."""
    stmt = select(func.count()).where(
        HistoricalSale.make == make,
        HistoricalSale.model == model,
    )
    return (await session.execute(stmt)).scalar_one()


async def value_private_listing(
    session: AsyncSession, listing: PrivateListing,
) -> None:
    """Compute and write valuation columns onto ``listing``.

    Caller controls the transaction. No network I/O — only DB reads and ORM
    mutations. ``listing`` must already be add()-ed / flushed so it has an id,
    but the caller holds the transaction.

    Private-sale pricing: ``ask_price_cad`` is treated as the bid with zero
    buyer premium and zero taxes. Landed cost uses the real provincial distance
    (same model as the auction valuator).
    """
    if listing.make is None or listing.model is None or listing.year is None:
        listing.valuation_status = ValuationStatus.INSUFFICIENT.value
        return

    comps = await build_comp_set(
        session,
        make=listing.make,
        model=listing.model,
        trim=listing.trim,
        year=listing.year,
        mileage_km=listing.mileage_km or 0,
    )
    fv = compute_fair_value(
        comps,
        condition=listing.condition_categorical or "decent",
        sparse=False,
    )

    listing.expected_value_cad = fv.expected_value_cad
    listing.confidence_bucket = fv.confidence.value

    hist = await _historical_comp_count(
        session, make=listing.make, model=listing.model,
    )

    listing.rarity_score = rarity_score(RarityInputs(
        desirable_trim_or_spec=listing.desirable_trim_or_spec,
        classic_or_collector=listing.classic_or_collector,
        historical_comp_count=hist,
        recent_appreciation=None,
    ))

    # PrivateListing has no description_quality column — pass None.
    listing.flag_score = flag_score(
        listing.red_flags or [],
        listing.green_flags or [],
        description_quality=None,
    )

    dest = listing.pickup_province or settings.home_province
    landed = landed_cost_premium(
        home=settings.home_province,
        dest=dest,
        distance_km=distance_km_between(settings.home_province, dest),
    )

    if listing.ask_price_cad is not None:
        listing.all_in_cost_cad = all_in_cost(
            current_high_bid=listing.ask_price_cad,
            buyer_premium_pct=_ZERO,
            gst_pct=_ZERO,
            pst_pct=_ZERO,
            landed_cost_premium=landed,
        )
        if fv.expected_value_cad is not None:
            listing.price_deal_score = price_deal_score(
                current_high_bid=listing.ask_price_cad,
                buyer_premium_pct=_ZERO,
                gst_pct=_ZERO,
                pst_pct=_ZERO,
                landed_cost_premium=landed,
                expected_value=fv.expected_value_cad,
            )

    if fv.confidence == ConfidenceBucket.INSUFFICIENT:
        listing.valuation_status = ValuationStatus.INSUFFICIENT.value
    else:
        listing.valuation_status = ValuationStatus.DONE.value
