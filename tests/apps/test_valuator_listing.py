"""S3 — the valuator prices a private listing (asking→sold haircut, no GST/BP)
and matches it against wants using the asking price + listing province."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.valuator.valuator import value_one
from carbuyer.db.enums import NotificationStatus, ValuationStatus
from carbuyer.db.models import HistoricalSale, PrivateListing, WantMatch
from carbuyer.scoring.asking_haircut import effective_acquisition_price
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria


def _seed_comps(session: AsyncSession, prices: list[int]) -> None:
    for p in prices:
        session.add(HistoricalSale(
            year=2015, mileage_km=150000,
            make="Toyota", model="Tacoma", trim=None,
            sale_channel="private", sale_platform="kijiji",
            title_status="NORMAL", schema_version=1,
            final_listed_price_cad=Decimal(p),
            final_price_with_premium_cad=Decimal(p),
            disposition_reason="sold",
        ))


def _seed_listing(session: AsyncSession, *, asking: str = "9000") -> PrivateListing:
    listing = PrivateListing(
        source="kijiji", source_listing_id="K1", url="http://k/1",
        title="2015 Toyota Tacoma", description="x" * 200,
        make="Toyota", model="Tacoma", year=2015, mileage_km=150000,
        asking_price_cad=Decimal(asking), seller_type="private",
        location_province="AB", listing_status="active",
    )
    session.add(listing)
    return listing


@pytest.mark.asyncio
async def test_value_one_prices_listing_with_haircut_and_no_gst(
    session: AsyncSession,
) -> None:
    _seed_comps(session, [12000, 12500, 11800, 12200, 11900, 12100])
    listing = _seed_listing(session, asking="9000")
    await session.flush()

    await value_one(session, listing)

    assert listing.valuation_status == ValuationStatus.DONE
    assert listing.price_deal_score is not None
    # all_in = asking*(1-haircut) + landed, with NO buyer premium and NO GST.
    expected_effective = effective_acquisition_price(Decimal("9000"), "private")
    landed = listing.landed_cost_premium_cad or Decimal("0")
    assert listing.all_in_at_current_bid_cad == expected_effective + landed
    # recommended_max_bid is a flipper concept — N/A for a fixed-price listing.
    assert listing.recommended_max_bid_cad is None


@pytest.mark.asyncio
async def test_value_one_matches_listing_against_want(session: AsyncSession) -> None:
    _seed_comps(session, [12000, 12500, 11800, 12200, 11900, 12100])
    listing = _seed_listing(session, asking="9000")
    want = await repo.create_want(
        session, name="tacoma", criteria=WantCriteria(makes=["Toyota"], models=["Tacoma"]),
    )
    await session.flush()

    await value_one(session, listing)

    assert listing.notification_status == NotificationStatus.PENDING
    row = (await session.execute(select(WantMatch))).scalar_one()
    assert row.search_id == want.id
    assert row.lot_id == listing.id


@pytest.mark.asyncio
async def test_value_one_no_want_match_when_make_differs(session: AsyncSession) -> None:
    _seed_comps(session, [12000, 12500, 11800, 12200, 11900, 12100])
    listing = _seed_listing(session, asking="9000")
    await repo.create_want(
        session, name="xterra", criteria=WantCriteria(makes=["Nissan"], models=["Xterra"]),
    )
    await session.flush()

    await value_one(session, listing)

    assert (await session.execute(select(WantMatch))).first() is None
