"""JTI behaviour for the vehicle_offer supertype/subtype split (S1).

These assert the joined-table-inheritance mapping itself: that an AuctionLot
writes both the parent (vehicle_offer) and child (auction_lot) rows under one
shared id, that the discriminator round-trips, that polymorphic loading returns
the right subclass, and that PrivateListing exists as an empty-capable sibling.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import (
    Auction,
    AuctionLot,
    PrivateListing,
    VehicleOffer,
)


async def _auction(session: AsyncSession) -> Auction:
    a = Auction(
        source="test", source_auction_id="A1", url="x", canonical_url="x",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()
    return a


def test_table_names_split() -> None:
    assert VehicleOffer.__tablename__ == "vehicle_offer"
    assert AuctionLot.__tablename__ == "auction_lot"
    assert PrivateListing.__tablename__ == "private_listing"
    # AuctionLot is a JTI subclass of the parent — same id space.
    assert issubclass(AuctionLot, VehicleOffer)
    assert issubclass(PrivateListing, VehicleOffer)


def test_parent_owns_shared_columns_child_owns_auction_columns() -> None:
    parent = {c.name for c in VehicleOffer.__table__.columns}
    child = {c.name for c in AuctionLot.__table__.columns}

    # Shared pipeline columns live on the parent.
    for col in (
        "offer_kind", "url", "title", "description", "photos", "parser_version",
        "year", "make", "model", "vin", "mileage_km",
        "price_deal_score", "expected_value_cad", "all_in_at_current_bid_cad",
        "enrichment_status", "valuation_status", "vision_status",
        "notification_status", "last_notified_channel",
        "user_action", "was_purchased_by_us", "created_at", "updated_at",
    ):
        assert col in parent, f"{col} should be on vehicle_offer"
        assert col not in child or col == "id", f"{col} should NOT be on auction_lot"

    # Auction-specific columns live on the child.
    for col in (
        "auction_id", "source_lot_id", "source_lot_row_id", "lot_number",
        "scheduled_end_at", "current_high_bid_cad", "bid_count_visible",
        "reserve_met", "lot_status", "final_bid_cad",
        "early_warning_notified_at", "cheap_notified_at", "closing_notified_at",
        "trajectory_notified_at", "extended_notified_at",
    ):
        assert col in child, f"{col} should be on auction_lot"
        assert col not in parent, f"{col} should NOT be on vehicle_offer"

    # Shared PK on the child references the parent.
    assert "id" in child


async def test_auction_lot_writes_both_tables_and_shares_id(
    session: AsyncSession,
) -> None:
    a = await _auction(session)
    lot = AuctionLot(
        auction_id=a.id, source_lot_id="L1", url="http://x/l1",
        make="Nissan", model="Xterra", year=2010,
        current_high_bid_cad=Decimal("8000"),
    )
    session.add(lot)
    await session.flush()
    lot_id = lot.id
    assert lot_id is not None
    # Discriminator written automatically from polymorphic_identity.
    assert lot.offer_kind == "auction"

    session.expire_all()
    # A parent row and a child row share the id.
    parent = (
        await session.execute(select(VehicleOffer).where(VehicleOffer.id == lot_id))
    ).scalar_one()
    # Polymorphic load returns the AuctionLot subclass with child columns intact.
    assert isinstance(parent, AuctionLot)
    assert parent.offer_kind == "auction"
    assert parent.make == "Nissan"
    assert parent.current_high_bid_cad == Decimal("8000")
    assert parent.source_lot_id == "L1"


async def test_private_listing_is_empty_capable_sibling(
    session: AsyncSession,
) -> None:
    listing = PrivateListing(
        url="http://x/listing/1", make="Lexus", model="GX 470", year=2005,
        asking_price_cad=Decimal("15000"), listing_status="active",
    )
    session.add(listing)
    await session.flush()
    lid = listing.id
    assert listing.offer_kind == "private"

    session.expire_all()
    loaded = (
        await session.execute(select(VehicleOffer).where(VehicleOffer.id == lid))
    ).scalar_one()
    assert isinstance(loaded, PrivateListing)
    assert loaded.asking_price_cad == Decimal("15000")
    assert loaded.make == "Lexus"
    # No auction child row exists for a private listing.
    n_auction = (
        await session.execute(
            select(func.count()).select_from(AuctionLot).where(AuctionLot.id == lid)
        )
    ).scalar_one()
    assert n_auction == 0
