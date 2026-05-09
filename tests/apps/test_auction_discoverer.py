from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.auction_discoverer.discoverer import upsert_auction
from carbuyer.db.models import Auction
from carbuyer.sources.base import AuctionRef, RawAuction


def _raw(title: str | None = "t1", **overrides: Any) -> RawAuction:
    base: dict[str, Any] = {
        "ref": AuctionRef(source="test", source_auction_id="A1", url="https://x/a/1"),
        "title": title,
        "description": None,
        "auctioneer_name": "A Co",
        "auctioneer_external_id": "ac1",
        "scheduled_start_at": None,
        "scheduled_end_at": None,
        "pickup_address": None,
        "pickup_city": None,
        "pickup_province": "AB",
        "pickup_window_text": None,
        "buyer_premium_pct": Decimal("0.10"),
        "online_bidding_fee_pct": None,
        "terms_text": None,
        "auction_subtype": "estate",
    }
    base.update(overrides)
    return RawAuction(**base)


@pytest.mark.asyncio
async def test_upsert_auction_inserts_then_updates(session: AsyncSession) -> None:
    a1 = await upsert_auction(session, _raw(title="t1"), discovered_via="hibid")
    await session.flush()
    assert a1.id is not None
    assert a1.discovered_via == ["hibid"]
    assert a1.title == "t1"

    a2 = await upsert_auction(
        session, _raw(title="t1-renamed"), discovered_via="hibid",
    )
    await session.flush()
    assert a2.id == a1.id
    assert a2.title == "t1-renamed"

    rows = (await session.execute(
        select(Auction).where(Auction.source == "test"),
    )).scalars().all()
    assert len(list(rows)) == 1


@pytest.mark.asyncio
async def test_upsert_auction_dedupes_discovered_via(session: AsyncSession) -> None:
    await upsert_auction(session, _raw(), discovered_via="hibid")
    await session.flush()
    await upsert_auction(session, _raw(), discovered_via="hibid")  # duplicate
    await session.flush()
    a = await upsert_auction(session, _raw(), discovered_via="farmauctionguide")
    await session.flush()
    assert sorted(a.discovered_via) == ["farmauctionguide", "hibid"]


@pytest.mark.asyncio
async def test_upsert_auction_does_not_overwrite_with_none(
    session: AsyncSession,
) -> None:
    await upsert_auction(session, _raw(title="t1"), discovered_via="hibid")
    await session.flush()
    a = await upsert_auction(session, _raw(title=None), discovered_via="hibid")
    await session.flush()
    assert a.title == "t1"  # original preserved


@pytest.mark.asyncio
async def test_upsert_auction_writes_canonical_url(session: AsyncSession) -> None:
    raw = _raw()
    a = await upsert_auction(session, raw, discovered_via="hibid")
    await session.flush()
    # canonicalize_url strips fragment + tracking + trailing slash + lowercases host.
    assert a.canonical_url == "https://x/a/1"
    assert a.url == raw.ref.url


@pytest.mark.asyncio
async def test_upsert_auction_refreshes_last_seen_at(session: AsyncSession) -> None:
    a1 = await upsert_auction(session, _raw(), discovered_via="hibid")
    await session.flush()
    first_seen = a1.first_seen_at
    last_seen_initial = a1.last_seen_at

    a2 = await upsert_auction(session, _raw(), discovered_via="hibid")
    await session.flush()
    assert a2.first_seen_at == first_seen
    assert a2.last_seen_at >= last_seen_initial
    # PG returns timezone as zoneinfo("Etc/UTC"); we just want awareness.
    assert isinstance(a2.last_seen_at, datetime)
    assert a2.last_seen_at.tzinfo is not None
