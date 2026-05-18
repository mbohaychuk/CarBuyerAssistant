"""Phase 10/11 deferred Task 43 — needs-plugin view + retry-routing endpoint.

resolve_platform() returns:
  - ("hibid", "<id>") / ("mcdougall", "<id>") for known platforms with an
    extractable auction id;
  - ("unknown:<host>", "<last-segment>") for unknown hosts;
  - None for known hosts whose URL has no auction id (footer/help/nav links).

retry_routing must handle all three branches without rerouting in the latter
two.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import Auction, AuctionLot


@pytest.fixture
def _patch_deps(  # pyright: ignore[reportUnusedFunction]
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _seed_unknown_auction(
    session: AsyncSession,
    *,
    source: str = "unknown:weirdplatform.com",
    source_auction_id: str = "abc",
    url: str = "https://weirdplatform.com/auction/abc",
    auctioneer_name: str | None = "Weird Auctions",
) -> Auction:
    a = Auction(
        source=source,
        source_auction_id=source_auction_id,
        url=url,
        canonical_url=url,
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        pickup_province="AB",
        auctioneer_name=auctioneer_name,
    )
    session.add(a)
    return a


# ─── /needs-plugin ───


@pytest.mark.asyncio
async def test_needs_plugin_view_renders_unknown_rows(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    _seed_unknown_auction(
        session, auctioneer_name="Random Co",
        source="unknown:random.example.com",
        url="https://random.example.com/sale/abc",
    )
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/needs-plugin")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Random Co" in r.text


@pytest.mark.asyncio
async def test_needs_plugin_view_excludes_known_sources(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    a = Auction(
        source="hibid",
        source_auction_id="H1",
        url="https://hibid.com/catalog/H1",
        canonical_url="https://hibid.com/catalog/H1",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        auctioneer_name="HiBid Co",
    )
    session.add(a)
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/needs-plugin")
    assert r.status_code == 200  # noqa: PLR2004
    assert "HiBid Co" not in r.text


@pytest.mark.asyncio
async def test_needs_plugin_view_empty(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/needs-plugin")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Nothing waiting" in r.text


# ─── /admin/auctions/{id}/retry_routing ───


@pytest.mark.asyncio
async def test_retry_routing_reroutes_when_plugin_now_matches(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    a = _seed_unknown_auction(
        session,
        source="unknown:terrymcdougall.hibid.com",
        source_auction_id="700001",
        url="https://terrymcdougall.hibid.com/catalog/700001/test",
    )
    # Seed one lot with non-pending statuses to prove the bulk-update runs.
    lot = AuctionLot(
        auction=a,
        source_lot_id="L1",
        url="https://terrymcdougall.hibid.com/catalog/700001/lot/1",
        title="2010 Ford F-150",
        year=2010, make="Ford", model="F-150",
        current_high_bid_cad=Decimal("8000"),
        enrichment_status=EnrichmentStatus.DONE.value,
        valuation_status=ValuationStatus.DONE.value,
        vision_status=VisionStatus.DONE.value,
        notification_status=NotificationStatus.DONE.value,
    )
    session.add(lot)
    await session.commit()
    auction_id = a.id
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(f"/admin/auctions/{auction_id}/retry_routing")
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(Auction, auction_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.source == "hibid"
    assert fresh.source_auction_id == "700001"
    assert fresh.routing_resolved_at is not None
    # needs_plugin_notified_at also stamped on resolution — the dashboard
    # action acknowledges the state, even when no Discord post fired earlier.
    assert fresh.needs_plugin_notified_at is not None

    # Lot statuses must have been reset to PENDING — proves the bulk-update
    # ran and the downstream pipeline will reprocess the lot under the new
    # source. Without this assertion, removing the update() block would
    # silently leave lots stranded.
    fresh_lot = await session.get(AuctionLot, lot_id)
    assert fresh_lot is not None
    await session.refresh(fresh_lot)
    assert fresh_lot.enrichment_status == EnrichmentStatus.PENDING.value
    assert fresh_lot.valuation_status == ValuationStatus.PENDING.value
    assert fresh_lot.vision_status == VisionStatus.PENDING.value
    assert fresh_lot.notification_status == NotificationStatus.PENDING.value


@pytest.mark.asyncio
async def test_retry_routing_409_when_target_source_already_exists(
    _patch_deps: AsyncSession,
) -> None:
    """If a row with (new_source, new_ext_id) already exists — typically
    because a direct discoverer surfaced the same auction in parallel — the
    UPDATE collides with the unique constraint. The endpoint must return 409
    rather than crash with HTTP 500, and the original unknown:* row must be
    left intact for ops to clean up."""
    session = _patch_deps
    # Pre-existing canonical row from the direct discoverer.
    canonical = Auction(
        source="hibid",
        source_auction_id="800001",
        url="https://terrymcdougall.hibid.com/catalog/800001/canonical",
        canonical_url="https://terrymcdougall.hibid.com/catalog/800001/canonical",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        auctioneer_name="HiBid Direct",
    )
    session.add(canonical)
    # Unknown:* duplicate left over from a now-removed upstream router.
    duplicate = _seed_unknown_auction(
        session,
        source="unknown:terrymcdougall.hibid.com",
        source_auction_id="800001",
        url="https://terrymcdougall.hibid.com/catalog/800001/duplicate",
    )
    await session.commit()
    duplicate_id = duplicate.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(f"/admin/auctions/{duplicate_id}/retry_routing")
    assert r.status_code == 409  # noqa: PLR2004

    # The unknown:* row must be unchanged after the rollback.
    fresh = await session.get(Auction, duplicate_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.source == "unknown:terrymcdougall.hibid.com"
    assert fresh.routing_resolved_at is None


@pytest.mark.asyncio
async def test_retry_routing_noop_when_still_unknown(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    a = _seed_unknown_auction(
        session,
        source="unknown:randomauctioneer.example.com",
        url="https://randomauctioneer.example.com/sale/abc",
    )
    await session.commit()
    auction_id = a.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(f"/admin/auctions/{auction_id}/retry_routing")
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(Auction, auction_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.source.startswith("unknown:")
    assert fresh.routing_resolved_at is None


@pytest.mark.asyncio
async def test_retry_routing_noop_when_known_host_without_auction_id(
    _patch_deps: AsyncSession,
) -> None:
    """Known hosts whose URL has no /catalog/<id> should NOT be rerouted —
    resolve_platform returns None for these (footer/help/nav links)."""
    session = _patch_deps
    a = _seed_unknown_auction(
        session,
        source="unknown:hibid.com",
        url="https://www.hibid.com/help",
    )
    await session.commit()
    auction_id = a.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(f"/admin/auctions/{auction_id}/retry_routing")
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(Auction, auction_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.source == "unknown:hibid.com"
    assert fresh.routing_resolved_at is None


@pytest.mark.asyncio
async def test_retry_routing_404_when_auction_missing(
    _patch_deps: AsyncSession,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/admin/auctions/999999/retry_routing")
    assert r.status_code == 404  # noqa: PLR2004
