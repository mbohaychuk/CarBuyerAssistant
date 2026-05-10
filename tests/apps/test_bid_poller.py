"""Tests for poller.py — _poll_one / _write_observation against the test DB.

Uses the same _patched_get_session pattern as test_enricher.py: patches
get_session on the poller module so fresh sessions share the test's outer
rolled-back transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.bid_poller import poller as poller_mod
from carbuyer.apps.bid_poller.poller import (  # pyright: ignore[reportPrivateUsage]
    _poll_one,
    _write_observation,
)
from carbuyer.db.enums import LotStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionBidHistory, AuctionLot
from carbuyer.sources.base import BidObservation, BidPoller, LotRef

# ── seed helpers ────────────────────────────────────────────────────────────


def _make_ref(*, source: str = "hibid") -> LotRef:
    return LotRef(
        source=source,
        source_auction_id="A1",
        source_lot_id="L1",
        url="https://hibid.com/catalog/A1",
    )


def _seed_lot(
    session: AsyncSession,
    *,
    lot_status: str = "open",
    current_high_bid_cad: Decimal | None = None,
    scheduled_end_at: datetime | None = None,
) -> tuple[Auction, AuctionLot]:
    end = scheduled_end_at or datetime(2026, 6, 1, tzinfo=UTC)
    a = Auction(
        source="hibid",
        source_auction_id="A1",
        url="https://hibid.com/catalog/A1",
        canonical_url="https://hibid.com/catalog/A1",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        scheduled_end_at=end,
        pickup_province="AB",
    )
    session.add(a)
    lot = AuctionLot(
        auction=a,
        source_lot_id="L1",
        url="https://hibid.com/catalog/A1",
        title="2010 Toyota Tundra",
        description="runs and drives",
        lot_status=lot_status,
        current_high_bid_cad=current_high_bid_cad,
    )
    return a, lot


def _obs(
    *,
    bid: Decimal | None = None,
    status: str = "open",
    end_time: datetime | None = None,
) -> BidObservation:
    return BidObservation(
        ref=_make_ref(),
        observed_at=datetime.now(UTC),
        current_high_bid_cad=bid,
        end_time_at_observation=end_time,
        status_at_observation=status,
    )


class _FakePoller(BidPoller):
    """Minimal BidPoller stub that returns a hand-built observation."""

    name = "hibid"
    version = "test"

    def __init__(self, obs: BidObservation | None = None, *, raises: bool = False) -> None:
        self._obs = obs
        self._raises = raises

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        if self._raises:
            raise RuntimeError("network failure")
        assert self._obs is not None
        return self._obs


# ── fixture ─────────────────────────────────────────────────────────────────


@pytest.fixture
def _patched_get_session(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    """Patch poller's get_session to use the test connection."""
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(poller_mod, "get_session", fake_get_session)
    return session


# ── _write_observation tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_observation_no_bid_change_does_not_flip_valuation_status(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    _, lot = _seed_lot(session, current_high_bid_cad=Decimal("1000.00"))
    session.add(lot)
    await session.flush()
    original_status = lot.valuation_status

    notified: list[tuple[str, str]] = []

    async def fake_notify(_session: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(poller_mod, "notify", fake_notify)

    obs = _obs(bid=Decimal("1000.00"), status="open")
    await _write_observation(lot.id, obs)
    await session.refresh(lot)

    assert lot.valuation_status == original_status
    assert lot.current_high_bid_cad == Decimal("1000.00")
    assert ("valuation_pending", str(lot.id)) not in notified


@pytest.mark.asyncio
async def test_write_observation_bid_change_sets_pending_and_records_history(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    _, lot = _seed_lot(session, current_high_bid_cad=Decimal("500.00"))
    session.add(lot)
    await session.flush()
    lot_id = lot.id

    notified: list[tuple[str, str]] = []

    async def fake_notify(_session: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(poller_mod, "notify", fake_notify)

    obs = _obs(bid=Decimal("750.00"), status="open")
    await _write_observation(lot_id, obs)
    await session.refresh(lot)

    assert lot.valuation_status == ValuationStatus.PENDING
    assert lot.current_high_bid_cad == Decimal("750.00")
    assert lot.last_bid_observed_at is not None

    stmt = select(AuctionBidHistory).where(AuctionBidHistory.lot_id == lot_id)
    result = (await session.execute(stmt)).scalars().all()
    assert len(result) == 1
    assert result[0].current_high_bid_cad == Decimal("750.00")

    assert notified == [("valuation_pending", str(lot_id))]


@pytest.mark.asyncio
async def test_write_observation_missing_status_closes_lot_preserves_bid(
    _patched_get_session: AsyncSession,
) -> None:
    """Lot disappeared from source — closed, final_bid_cad = last recorded bid."""
    session = _patched_get_session
    _, lot = _seed_lot(session, current_high_bid_cad=Decimal("1200.00"))
    session.add(lot)
    await session.flush()

    obs = _obs(bid=None, status="missing")
    await _write_observation(lot.id, obs)
    await session.refresh(lot)

    assert lot.lot_status == LotStatus.CLOSED
    assert lot.closed_at is not None
    # final_bid_cad must not crash (the unbound-name bug) and must hold last bid
    assert lot.final_bid_cad == Decimal("1200.00")


@pytest.mark.asyncio
async def test_write_observation_closed_status_sets_final_bid(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    _, lot = _seed_lot(session)
    session.add(lot)
    await session.flush()

    obs = _obs(bid=Decimal("900.00"), status="closed")
    await _write_observation(lot.id, obs)
    await session.refresh(lot)

    assert lot.lot_status == LotStatus.CLOSED
    assert lot.closed_at is not None
    assert lot.final_bid_cad == Decimal("900.00")


@pytest.mark.asyncio
async def test_write_observation_extended_end_time_marks_lot_extended(
    _patched_get_session: AsyncSession,
) -> None:
    original_end = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    extended_end = original_end + timedelta(minutes=5)

    session = _patched_get_session
    auction, lot = _seed_lot(session, scheduled_end_at=original_end)
    session.add(lot)
    await session.flush()

    obs = _obs(bid=Decimal("500.00"), status="open", end_time=extended_end)
    await _write_observation(lot.id, obs)
    await session.refresh(lot)
    await session.refresh(auction)

    assert lot.lot_status == LotStatus.EXTENDED
    assert auction.last_seen_end_at == extended_end


# ── _poll_one tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_one_unknown_source_returns_without_writing(
    _patched_get_session: AsyncSession,
) -> None:
    """No poller for this source — silently skip without touching DB."""
    session = _patched_get_session
    _, lot = _seed_lot(session)
    session.add(lot)
    await session.flush()

    ref = LotRef(source="unknown_source", source_auction_id="A1", source_lot_id="L1", url="x")
    pollers: dict[str, BidPoller] = {}
    # Must not raise; lot untouched
    await _poll_one(lot.id, ref, pollers=pollers)
    await session.refresh(lot)
    assert lot.lot_status == LotStatus.OPEN


@pytest.mark.asyncio
async def test_poll_one_poll_raises_logs_and_does_not_write(
    _patched_get_session: AsyncSession,
) -> None:
    """Exception in poll_bid is caught; no DB write happens."""
    session = _patched_get_session
    _, lot = _seed_lot(session, current_high_bid_cad=Decimal("400.00"))
    session.add(lot)
    await session.flush()
    original_bid = lot.current_high_bid_cad

    fake = _FakePoller(raises=True)
    pollers: dict[str, BidPoller] = {"hibid": fake}
    ref = _make_ref()

    await _poll_one(lot.id, ref, pollers=pollers)  # must not raise
    await session.refresh(lot)

    assert lot.current_high_bid_cad == original_bid  # unchanged


@pytest.mark.asyncio
async def test_poll_one_happy_path_writes_observation(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success path: poller returns observation → lot bid updated + history row written."""
    session = _patched_get_session
    _, lot = _seed_lot(session, current_high_bid_cad=Decimal("400.00"))
    session.add(lot)
    await session.flush()
    lot_id = lot.id

    notified: list[tuple[str, str]] = []

    async def fake_notify(_session: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(poller_mod, "notify", fake_notify)

    obs = _obs(bid=Decimal("500.00"), status="open")
    pollers: dict[str, BidPoller] = {"hibid": _FakePoller(obs)}
    ref = _make_ref()

    await _poll_one(lot_id, ref, pollers=pollers)
    await session.refresh(lot)

    assert lot.current_high_bid_cad == Decimal("500.00")

    stmt = select(AuctionBidHistory).where(AuctionBidHistory.lot_id == lot_id)
    rows = (await session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].current_high_bid_cad == Decimal("500.00")
    assert rows[0].status_at_observation == "open"

    assert notified == [("valuation_pending", str(lot_id))]
