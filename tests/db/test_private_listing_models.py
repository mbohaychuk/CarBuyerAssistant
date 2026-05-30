from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import PrivateListing


def _listing(**ov: object) -> PrivateListing:
    base: dict[str, object] = dict(
        source="kijiji", source_listing_id="L1",
        url="https://kijiji.ca/v/1", canonical_url="https://kijiji.ca/v/1",
    )
    base.update(ov)
    return PrivateListing(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_private_listing_defaults(session: AsyncSession) -> None:
    pl = _listing(make="Ford", model="Mustang", ask_price_cad=Decimal("18000"))
    session.add(pl)
    await session.flush()
    await session.refresh(pl)
    assert pl.id is not None
    assert pl.title_status == "UNKNOWN"          # server_default
    assert pl.photos == []                        # text[] default {}
    assert pl.red_flags == [] and pl.green_flags == []
    assert pl.enrichment_status == "pending" and pl.valuation_status == "pending"
    assert pl.first_seen_at is not None and pl.last_seen_at is not None
    assert pl.removed_at is None and pl.alerted_at is None
    assert pl.created_at is not None


@pytest.mark.asyncio
async def test_private_listing_unique_source(session: AsyncSession) -> None:
    session.add(_listing(source="kijiji", source_listing_id="DUP"))
    await session.flush()
    session.add(_listing(source="kijiji", source_listing_id="DUP"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_private_listing_roundtrips_valuator_and_array_fields(
    session: AsyncSession,
) -> None:
    pl = _listing(
        make="Dodge", model="Viper", year=2005, mileage_km=40000,
        ask_price_cad=Decimal("55000"), photos=["a.jpg", "b.jpg"],
        rarity_score=4.0, expected_value_cad=Decimal("60000"),
        all_in_cost_cad=Decimal("56000"), price_deal_score=0.30,
        pickup_province="AB",
    )
    session.add(pl)
    await session.flush()
    await session.refresh(pl)
    assert pl.photos == ["a.jpg", "b.jpg"]
    assert pl.rarity_score == 4.0  # noqa: PLR2004
    assert pl.price_deal_score == 0.30  # noqa: PLR2004
    assert pl.pickup_province == "AB"
