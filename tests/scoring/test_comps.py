from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Auction, AuctionLot, HistoricalSale, PrivateListing
from carbuyer.scoring.comps import ComparableSale, build_comp_set


@pytest.mark.asyncio
async def test_build_comp_set_includes_disappeared_private_listings(
    session: AsyncSession,
) -> None:
    # A sold/removed private listing's last asking is a noisy sold proxy → a comp
    # at the private (1.00) channel. An ACTIVE listing is not a sale → excluded.
    sold = PrivateListing(
        source="kijiji", source_listing_id="K1", url="http://k/1",
        title="2015 Toyota Tacoma", year=2015, make="Toyota", model="Tacoma",
        mileage_km=150000, asking_price_cad=Decimal("17500"),
        listing_status="sold", days_on_market=20,
    )
    active = PrivateListing(
        source="kijiji", source_listing_id="K2", url="http://k/2",
        title="2015 Toyota Tacoma", year=2015, make="Toyota", model="Tacoma",
        mileage_km=150000, asking_price_cad=Decimal("19000"),
        listing_status="active",
    )
    session.add_all([sold, active])
    await session.commit()

    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim=None, year=2015, mileage_km=150000,
    )
    private_comps = [c for c in comps if c.source == "private_listing"]
    assert len(private_comps) == 1
    assert private_comps[0].sale_channel == "private"
    assert private_comps[0].price_cad == Decimal("17500")


def _historical(**overrides: object) -> HistoricalSale:
    base: dict[str, object] = dict(
        make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000,
        sale_channel="auction_estate", sale_platform="hibid",
        title_status="NORMAL", schema_version=1,
        final_listed_price_cad=Decimal("20000"),
        final_price_with_premium_cad=Decimal("22000"),
        buyer_premium_pct_at_sale=Decimal("0.10"),
        disposition_reason="sold",
    )
    base.update(overrides)
    return HistoricalSale(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_build_comp_set_filters_make_model_year_mileage(
    session: AsyncSession,
) -> None:
    session.add_all([
        _historical(year=2015, mileage_km=150000),
        _historical(year=2014, mileage_km=160000),
        # Out of year window:
        _historical(year=2010, mileage_km=150000,
                    final_price_with_premium_cad=Decimal("9000")),
        # Out of mileage band:
        _historical(year=2015, mileage_km=400000,
                    final_price_with_premium_cad=Decimal("8000")),
        # Wrong model:
        _historical(model="Tundra"),
    ])
    await session.commit()

    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000, year_window=1, mileage_pct=0.20,
    )
    assert len(comps) == 2  # noqa: PLR2004
    assert all(isinstance(c, ComparableSale) for c in comps)
    assert all(c.source == "historical_sales" for c in comps)


@pytest.mark.asyncio
async def test_build_comp_set_trim_filter_includes_null_trims(
    session: AsyncSession,
) -> None:
    # When the lot has a trim, untrimmed historical sales are still useful
    # comps — base-trim listings often omit trim. Explicitly different trims
    # are excluded.
    session.add_all([
        _historical(trim="TRD Off-Road"),
        _historical(trim=None),
        _historical(trim="TRD Sport"),
    ])
    await session.commit()
    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000,
    )
    assert len(comps) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_build_comp_set_no_trim_argument_skips_filter(
    session: AsyncSession,
) -> None:
    session.add_all([
        _historical(trim="TRD Pro"),
        _historical(trim=None),
        _historical(trim="SR5"),
    ])
    await session.commit()
    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim=None,
        year=2015, mileage_km=150000,
    )
    assert len(comps) == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_build_comp_set_includes_recent_closed_auction_lots(
    session: AsyncSession,
) -> None:
    # Closed-and-sold auction_lots within recency window contribute as comps.
    # The query joins on the same make/model/year/mileage filter; sale_channel
    # for a contributed AuctionLot is hard-coded "auction_estate" since the
    # MVP only ingests estate-class auction sources.
    a = Auction(
        source="hibid", source_auction_id="A1", url="https://x/a1",
        canonical_url="https://x/a1", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()

    recent = AuctionLot(
        auction_id=a.id, source_lot_id="L1", url="https://x/lot/1",
        title="2015 Toyota Tacoma TRD Off-Road",
        year=2015, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        mileage_km=150000,
        lot_status="closed",
        closed_at=datetime.now(UTC) - timedelta(days=5),
        final_bid_cad=Decimal("18000"),
    )
    stale = AuctionLot(
        auction_id=a.id, source_lot_id="L2", url="https://x/lot/2",
        title="2015 Toyota Tacoma TRD Off-Road",
        year=2015, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        mileage_km=150000,
        lot_status="closed",
        closed_at=datetime.now(UTC) - timedelta(days=60),  # too old
        final_bid_cad=Decimal("17000"),
    )
    open_lot = AuctionLot(
        auction_id=a.id, source_lot_id="L3", url="https://x/lot/3",
        title="2015 Toyota Tacoma TRD Off-Road",
        year=2015, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        mileage_km=150000,
        lot_status="open",  # not closed
        final_bid_cad=Decimal("16000"),
    )
    session.add_all([recent, stale, open_lot])
    await session.commit()

    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000,
    )
    auction_comps = [c for c in comps if c.source == "auction_lots"]
    assert len(auction_comps) == 1
    assert auction_comps[0].price_cad == Decimal("18000")


@pytest.mark.asyncio
async def test_build_comp_set_uses_listed_price_when_premium_missing(
    session: AsyncSession,
) -> None:
    session.add(_historical(
        final_listed_price_cad=Decimal("15000"),
        final_price_with_premium_cad=None,
    ))
    await session.commit()

    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000,
    )
    assert len(comps) == 1
    assert comps[0].price_cad == Decimal("15000")


@pytest.mark.asyncio
async def test_build_comp_set_skips_rows_with_no_price(
    session: AsyncSession,
) -> None:
    session.add(_historical(
        final_listed_price_cad=None, final_price_with_premium_cad=None,
    ))
    await session.commit()

    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000,
    )
    assert len(comps) == 0


@pytest.mark.asyncio
async def test_build_comp_set_includes_force_closed_lots(
    session: AsyncSession,
) -> None:
    """bid_poller force-closes lots stuck OPEN past scheduled_end+24h and
    writes final_bid_cad from current_high_bid_cad in the same branch. Those
    are real completed sales — filtering `lot_status == 'closed'` alone
    silently drops them and shrinks an already-sparse Western-Canada comp set.
    """
    a = Auction(
        source="hibid", source_auction_id="A1", url="x", canonical_url="x",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()
    force_closed = AuctionLot(
        auction_id=a.id, source_lot_id="L1", url="https://x/lot/1",
        title="2015 Toyota Tacoma TRD Off-Road",
        year=2015, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        mileage_km=150000,
        lot_status="force_closed",
        closed_at=datetime.now(UTC) - timedelta(days=3),
        final_bid_cad=Decimal("19500"),
    )
    session.add(force_closed)
    await session.commit()

    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000,
    )
    auction_comps = [c for c in comps if c.source == "auction_lots"]
    assert len(auction_comps) == 1
    assert auction_comps[0].price_cad == Decimal("19500")


@pytest.mark.asyncio
async def test_build_comp_set_make_model_lookup_is_case_insensitive(
    session: AsyncSession,
) -> None:
    """Enricher writes the LLM's casing; historical_sales seeded from
    external research carries whatever casing the researcher used. The
    comp filter upper-cases both sides so the lookup can't collapse to
    zero on a cosmetic mismatch."""
    session.add_all([
        _historical(make="toyota", model="tacoma"),
        _historical(make="TOYOTA", model="TACOMA"),
    ])
    await session.commit()
    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000,
    )
    assert len(comps) == 2  # noqa: PLR2004
