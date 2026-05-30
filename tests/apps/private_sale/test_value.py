"""Tests for value_private_listing.

Verifies:
  - A well-seeded comp set + underpriced ask → all valuation columns written,
    price_deal_score positive, all_in_cost_cad = ask + landed (no premium/tax),
    valuation_status='done'.
  - make=None → valuation_status='insufficient', no crash.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.private_sale.value import value_private_listing
from carbuyer.db.models import HistoricalSale, PrivateListing

# Named constants — PLR2004 guard.
_YEAR = 2015
_MILEAGE_KM = 150_000
_ASK_PRICE_UNDERPRICED = Decimal("8000")   # well below the ~14k expected value
_COMP_PRICES = [10_000, 11_000, 12_000, 13_000, 14_000,
                15_000, 16_000, 17_000, 18_000, 19_000]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_comps(session: AsyncSession, prices: list[int]) -> None:
    """Mirror tests/apps/test_valuator.py `_seed_comps`."""
    for p in prices:
        session.add(HistoricalSale(
            year=_YEAR, mileage_km=_MILEAGE_KM,
            make="Toyota", model="Tacoma", trim=None,
            sale_channel="auction_estate", sale_platform="hibid",
            title_status="NORMAL", schema_version=1,
            final_listed_price_cad=Decimal(p),
            final_price_with_premium_cad=Decimal(p),
            buyer_premium_pct_at_sale=Decimal("0.10"),
            disposition_reason="sold",
        ))


def _make_enriched_listing(*, ask: Decimal = _ASK_PRICE_UNDERPRICED) -> PrivateListing:
    """PrivateListing with all enricher outputs filled in, ready for valuation."""
    listing = PrivateListing()
    listing.source = "kijiji"
    listing.source_listing_id = "value-test-001"
    listing.url = "https://www.kijiji.ca/v-cars-trucks/edmonton/test/9999"
    listing.canonical_url = "https://www.kijiji.ca/v-cars-trucks/edmonton/test/9999"
    listing.title = "2015 Toyota Tacoma"
    listing.description = "Solid truck, well maintained."
    listing.photos = []
    listing.year = _YEAR
    listing.make = "Toyota"
    listing.model = "Tacoma"
    listing.trim = None
    listing.mileage_km = _MILEAGE_KM
    listing.ask_price_cad = ask
    listing.pickup_province = "AB"
    listing.condition_categorical = "decent"
    listing.red_flags = []
    listing.green_flags = []
    listing.showstopper_flags = []
    listing.desirable_trim_or_spec = False
    listing.classic_or_collector = False
    listing.enrichment_status = "done"
    listing.valuation_status = "pending"
    listing.title_status = "NORMAL"
    return listing


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_value_private_listing_full(session: AsyncSession) -> None:
    """Well-seeded comps + underpriced ask → all valuation columns set, status=done."""
    _seed_comps(session, _COMP_PRICES)
    listing = _make_enriched_listing()
    session.add(listing)
    await session.commit()

    await value_private_listing(session, listing)
    await session.commit()

    assert listing.valuation_status == "done"
    assert listing.expected_value_cad is not None
    assert listing.confidence_bucket is not None
    assert listing.rarity_score is not None
    assert listing.flag_score is not None

    # all_in_cost = ask + landed (no premium, no tax).
    # AB→AB distance is 0 so landed = 0, meaning all_in_cost = ask.
    assert listing.all_in_cost_cad is not None
    assert listing.all_in_cost_cad == listing.ask_price_cad  # zero landed AB→AB

    # Underpriced ask should yield a positive deal score.
    assert listing.price_deal_score is not None
    assert listing.price_deal_score > 0


@pytest.mark.asyncio
async def test_value_private_listing_out_of_province(session: AsyncSession) -> None:
    """Pickup in SK from AB home: landed_cost > 0, all_in_cost = ask + landed."""
    _seed_comps(session, _COMP_PRICES)
    listing = _make_enriched_listing()
    listing.source_listing_id = "value-test-002"
    listing.pickup_province = "SK"
    session.add(listing)
    await session.commit()

    await value_private_listing(session, listing)
    await session.commit()

    assert listing.valuation_status == "done"
    assert listing.all_in_cost_cad is not None
    # SK→AB has a non-zero landed cost, so all_in_cost > ask.
    assert listing.all_in_cost_cad > listing.ask_price_cad


@pytest.mark.asyncio
async def test_value_private_listing_no_make_sets_insufficient(
    session: AsyncSession,
) -> None:
    """make=None → valuation_status='insufficient', no other columns written, no crash."""
    listing = _make_enriched_listing()
    listing.make = None
    listing.source_listing_id = "value-test-003"
    session.add(listing)
    await session.commit()

    await value_private_listing(session, listing)
    await session.commit()

    assert listing.valuation_status == "insufficient"
    # Valuator bailed early — none of the scoring outputs should be set.
    assert listing.expected_value_cad is None
    assert listing.price_deal_score is None
    assert listing.all_in_cost_cad is None


@pytest.mark.asyncio
async def test_value_private_listing_insufficient_comps(session: AsyncSession) -> None:
    """Fewer than INSUFFICIENT_COMPS_THRESHOLD comps → status=insufficient, no expected_value
    or price_deal_score, but landed-cost all_in_cost_cad still computed."""
    from carbuyer.scoring.fair_value import INSUFFICIENT_COMPS_THRESHOLD  # noqa: PLC0415

    # Seed one fewer comp than the threshold so confidence lands INSUFFICIENT.
    _seed_comps(session, _COMP_PRICES[: INSUFFICIENT_COMPS_THRESHOLD - 1])
    listing = _make_enriched_listing()
    listing.source_listing_id = "value-test-005"
    session.add(listing)
    await session.commit()

    await value_private_listing(session, listing)
    await session.commit()

    assert listing.valuation_status == "insufficient"
    assert listing.expected_value_cad is None
    assert listing.price_deal_score is None
    # Landed cost runs regardless of comp confidence; AB→AB is zero so all_in = ask.
    assert listing.all_in_cost_cad is not None


@pytest.mark.asyncio
async def test_value_private_listing_no_ask_price(session: AsyncSession) -> None:
    """ask_price_cad=None → expected_value still set, all_in_cost/price_deal_score None."""
    _seed_comps(session, _COMP_PRICES)
    listing = _make_enriched_listing(ask=_ASK_PRICE_UNDERPRICED)
    listing.ask_price_cad = None
    listing.source_listing_id = "value-test-004"
    session.add(listing)
    await session.commit()

    await value_private_listing(session, listing)
    await session.commit()

    assert listing.valuation_status == "done"
    assert listing.expected_value_cad is not None
    assert listing.all_in_cost_cad is None
    assert listing.price_deal_score is None
