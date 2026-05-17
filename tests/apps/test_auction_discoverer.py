from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import carbuyer.apps.auction_discoverer.discoverer as discoverer_mod
from carbuyer.apps.auction_discoverer.discoverer import _sweep_one_discoverer, upsert_auction
from carbuyer.db.models import Auction
from carbuyer.sources.base import AuctionDiscoverer, AuctionRef, RawAuction


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


# ── _sweep_one_discoverer needs_plugin NOTIFY branch ─────────────────────────


class _FakeDiscoverer(AuctionDiscoverer):
    """In-test discoverer that emits a fixed list of refs without HTTP."""

    name: ClassVar[str] = "fake_router"
    version: ClassVar[str] = "1"

    def __init__(self, refs: list[AuctionRef]) -> None:
        self._refs = refs

    async def discover_auctions(self) -> AsyncIterator[AuctionRef]:
        for ref in self._refs:
            yield ref


@pytest.mark.asyncio
async def test_sweep_emits_needs_plugin_notify_for_unknown_source(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wire test session as the get_session yielded inside _sweep_one_discoverer.
    notified: list[tuple[str, str]] = []

    async def fake_notify(_sess: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(discoverer_mod, "notify", fake_notify)

    from contextlib import asynccontextmanager  # noqa: PLC0415

    @asynccontextmanager
    async def _patched_get_session() -> AsyncIterator[AsyncSession]:
        maker = session.info["maker"]
        async with maker() as s:
            yield s

    monkeypatch.setattr(discoverer_mod, "get_session", _patched_get_session)

    ref = AuctionRef(
        source="unknown:randomauctioneer.example.com",
        source_auction_id="abc",
        url="https://randomauctioneer.example.com/sale/abc",
    )
    await _sweep_one_discoverer(_FakeDiscoverer([ref]), fetchers={})

    # Both notifies fire on first sight of an unknown auction:
    # auction_pending (always) AND needs_plugin (gated on first-time).
    channels = [c for (c, _payload) in notified]
    assert "auction_pending" in channels
    assert "needs_plugin" in channels


@pytest.mark.asyncio
async def test_sweep_does_not_re_emit_needs_plugin_for_already_notified(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pre-seed an unknown auction already stamped as notified.
    pre = await upsert_auction(
        session,
        _raw(),  # source="test", source_auction_id="A1"
        discovered_via="prior_sweep",
    )
    pre.source = "unknown:already-known.example.com"
    pre.needs_plugin_notified_at = datetime.now(UTC)
    await session.flush()
    await session.commit()

    notified: list[tuple[str, str]] = []

    async def fake_notify(_sess: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(discoverer_mod, "notify", fake_notify)

    from contextlib import asynccontextmanager  # noqa: PLC0415

    @asynccontextmanager
    async def _patched_get_session() -> AsyncIterator[AsyncSession]:
        maker = session.info["maker"]
        async with maker() as s:
            yield s

    monkeypatch.setattr(discoverer_mod, "get_session", _patched_get_session)

    ref = AuctionRef(
        source="unknown:already-known.example.com",
        source_auction_id="A1",  # same as pre-seeded
        url="https://already-known.example.com/sale/abc",
    )
    await _sweep_one_discoverer(_FakeDiscoverer([ref]), fetchers={})

    # auction_pending always fires; needs_plugin must NOT (already stamped).
    channels = [c for (c, _payload) in notified]
    assert "auction_pending" in channels
    assert "needs_plugin" not in channels


@pytest.mark.asyncio
async def test_sweep_does_not_emit_needs_plugin_for_known_source(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Known source (e.g. hibid) — even though no fetcher is registered in this
    # test (so minimal_raw_auction is used), the source is NOT unknown:* so
    # needs_plugin must not fire.
    notified: list[tuple[str, str]] = []

    async def fake_notify(_sess: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(discoverer_mod, "notify", fake_notify)

    from contextlib import asynccontextmanager  # noqa: PLC0415

    @asynccontextmanager
    async def _patched_get_session() -> AsyncIterator[AsyncSession]:
        maker = session.info["maker"]
        async with maker() as s:
            yield s

    monkeypatch.setattr(discoverer_mod, "get_session", _patched_get_session)

    ref = AuctionRef(
        source="hibid",
        source_auction_id="700001",
        url="https://terrymcdougall.hibid.com/catalog/700001/sale",
    )
    await _sweep_one_discoverer(_FakeDiscoverer([ref]), fetchers={})

    channels = [c for (c, _payload) in notified]
    assert "auction_pending" in channels
    assert "needs_plugin" not in channels
