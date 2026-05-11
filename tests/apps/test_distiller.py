"""Tests for auction_distiller.distiller — distill_lot and main().

Uses the _patched_get_session fixture pattern from test_vision_batcher.py:
patches get_session on the distiller module so sessions opened inside main()
share the test's outer rolled-back transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.auction_distiller import distiller as distiller_mod
from carbuyer.apps.auction_distiller.distiller import (
    DISTILL_AGE_DAYS,
    distill_lot,
    main,
)
from carbuyer.db.enums import LotStatus, UserAction
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale

# ── helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
# One day past the distill cutoff — eligible by age.
_OLD_CLOSED = _NOW - timedelta(days=DISTILL_AGE_DAYS + 1)
# Within DISTILL_AGE_DAYS — too recent to distill.
_RECENT_CLOSED = _NOW - timedelta(days=DISTILL_AGE_DAYS - 9)


def _make_auction(
    session: AsyncSession,
    *,
    source: str = "test",
    source_auction_id: str = "A1",
    auction_subtype: str = "estate",
    buyer_premium_pct: Decimal | None = Decimal("0.10"),
    pickup_province: str | None = "AB",
    pickup_city: str | None = "Calgary",
) -> Auction:
    a = Auction(
        source=source,
        source_auction_id=source_auction_id,
        url="https://x",
        canonical_url="https://x",
        auction_subtype=auction_subtype,
        first_seen_at=_NOW - timedelta(days=30),
        last_seen_at=_NOW - timedelta(days=20),
        scheduled_end_at=_NOW - timedelta(days=20),
        pickup_province=pickup_province,
        pickup_city=pickup_city,
        buyer_premium_pct=buyer_premium_pct,
    )
    session.add(a)
    return a


def _make_lot(
    session: AsyncSession,
    auction: Auction,
    *,
    source_lot_id: str = "L1",
    lot_status: str = LotStatus.CLOSED,
    closed_at: datetime | None = None,
    final_bid_cad: Decimal | None = Decimal("8000.00"),
    user_action: str | None = None,
    was_purchased_by_us: bool = False,
    cheap_notified_at: datetime | None = None,
    early_warning_notified_at: datetime | None = None,
    closing_notified_at: datetime | None = None,
    trajectory_notified_at: datetime | None = None,
    extended_notified_at: datetime | None = None,
) -> AuctionLot:
    lot = AuctionLot(
        auction=auction,
        source_lot_id=source_lot_id,
        url=f"https://x/lot/{source_lot_id}",
        title="2010 Toyota Tundra",
        description="runs fine",
        year=2010,
        make="Toyota",
        model="Tundra",
        lot_status=lot_status,
        closed_at=closed_at if closed_at is not None else _OLD_CLOSED,
        final_bid_cad=final_bid_cad,
        user_action=user_action,
        was_purchased_by_us=was_purchased_by_us,
        cheap_notified_at=cheap_notified_at,
        early_warning_notified_at=early_warning_notified_at,
        closing_notified_at=closing_notified_at,
        trajectory_notified_at=trajectory_notified_at,
        extended_notified_at=extended_notified_at,
    )
    session.add(lot)
    return lot


# ── fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
def _patched_get_session(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    """Patch distiller's get_session to use the test connection."""
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(distiller_mod, "get_session", fake_get_session)
    return session


# ── distill_lot unit tests ────────────────────────────────────────────────────


async def test_distill_lot_creates_historical_sale(session: AsyncSession) -> None:
    """Happy path: sold lot produces a HistoricalSale with correct field mapping."""
    auction = _make_auction(session, buyer_premium_pct=Decimal("0.10"))
    lot = _make_lot(session, auction, final_bid_cad=Decimal("8000.00"))
    await session.flush()

    await distill_lot(session, lot, auction)
    await session.flush()

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 1
    sale = sales[0]

    assert sale.make == "Toyota"
    assert sale.model == "Tundra"
    assert sale.year == 2010  # noqa: PLR2004
    assert sale.sale_channel == "auction_estate"
    assert sale.sale_platform == "test"
    assert sale.seller_province == "AB"
    assert sale.seller_city == "Calgary"
    assert sale.final_listed_price_cad == Decimal("8000.00")
    # 8000 * 1.10 = 8800.00 (Numeric(12,2) rounds to 2 decimal places after DB roundtrip)
    assert sale.final_price_with_premium_cad == Decimal("8800.00")
    assert sale.buyer_premium_pct_at_sale == Decimal("0.10")
    assert sale.disposition_reason == "sold"
    assert sale.schema_version == 1


async def test_distill_lot_unsold_disposition(session: AsyncSession) -> None:
    """Lot with final_bid_cad=None → disposition_reason='unsold', no premium calc."""
    auction = _make_auction(session, source_auction_id="A2")
    lot = _make_lot(session, auction, source_lot_id="L2", final_bid_cad=None)
    await session.flush()

    await distill_lot(session, lot, auction)
    await session.flush()

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 1
    assert sales[0].disposition_reason == "unsold"
    assert sales[0].final_listed_price_cad is None
    assert sales[0].final_price_with_premium_cad is None


async def test_distill_lot_was_notified_true_when_cheap_notified_set(
    session: AsyncSession,
) -> None:
    auction = _make_auction(session, source_auction_id="A3")
    lot = _make_lot(
        session,
        auction,
        source_lot_id="L3",
        cheap_notified_at=_NOW - timedelta(days=25),
    )
    await session.flush()

    await distill_lot(session, lot, auction)
    await session.flush()

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert sales[0].was_notified is True


async def test_distill_lot_was_notified_true_when_early_warning_set(
    session: AsyncSession,
) -> None:
    auction = _make_auction(session, source_auction_id="A4")
    lot = _make_lot(
        session,
        auction,
        source_lot_id="L4",
        early_warning_notified_at=_NOW - timedelta(days=25),
    )
    await session.flush()

    await distill_lot(session, lot, auction)
    await session.flush()

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert sales[0].was_notified is True


async def test_distill_lot_was_notified_true_when_closing_notified_set(
    session: AsyncSession,
) -> None:
    auction = _make_auction(session, source_auction_id="A5")
    lot = _make_lot(
        session,
        auction,
        source_lot_id="L5",
        closing_notified_at=_NOW - timedelta(days=25),
    )
    await session.flush()

    await distill_lot(session, lot, auction)
    await session.flush()

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert sales[0].was_notified is True


async def test_distill_lot_was_notified_false_when_no_notification(
    session: AsyncSession,
) -> None:
    auction = _make_auction(session, source_auction_id="A6")
    lot = _make_lot(session, auction, source_lot_id="L6")
    await session.flush()

    await distill_lot(session, lot, auction)
    await session.flush()

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert sales[0].was_notified is False


async def test_distill_lot_deletes_lot_row(session: AsyncSession) -> None:
    """distill_lot removes the AuctionLot row (caller commits)."""
    auction = _make_auction(session, source_auction_id="A7")
    lot = _make_lot(session, auction, source_lot_id="L7")
    await session.flush()
    lot_id = lot.id

    await distill_lot(session, lot, auction)
    await session.flush()

    remaining = (
        await session.execute(sa_select(AuctionLot).where(AuctionLot.id == lot_id))
    ).scalar_one_or_none()
    assert remaining is None


async def test_distill_lot_no_premium_when_buyer_premium_none(
    session: AsyncSession,
) -> None:
    """Auction with no buyer_premium_pct → final_price_with_premium_cad is None."""
    auction = _make_auction(session, source_auction_id="A8", buyer_premium_pct=None)
    lot = _make_lot(session, auction, source_lot_id="L8", final_bid_cad=Decimal("5000.00"))
    await session.flush()

    await distill_lot(session, lot, auction)
    await session.flush()

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert sales[0].final_price_with_premium_cad is None
    assert sales[0].final_listed_price_cad == Decimal("5000.00")


# ── main() integration tests ──────────────────────────────────────────────────


async def test_main_skips_recently_closed(
    _patched_get_session: AsyncSession,
) -> None:
    """Lot closed 5 days ago (within DISTILL_AGE_DAYS) is not distilled."""
    session = _patched_get_session
    auction = _make_auction(session)
    _make_lot(session, auction, closed_at=_RECENT_CLOSED)
    await session.flush()

    await main(now=_NOW)

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 0


async def test_main_skips_open_lot(
    _patched_get_session: AsyncSession,
) -> None:
    """lot_status=OPEN is not distilled even if closed_at is old."""
    session = _patched_get_session
    auction = _make_auction(session)
    _make_lot(session, auction, lot_status=LotStatus.OPEN)
    await session.flush()

    await main(now=_NOW)

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 0


async def test_main_skips_purchased_by_us(
    _patched_get_session: AsyncSession,
) -> None:
    """Lots we purchased are never distilled — they live in purchases table."""
    session = _patched_get_session
    auction = _make_auction(session)
    _make_lot(session, auction, was_purchased_by_us=True)
    await session.flush()

    await main(now=_NOW)

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 0


async def test_main_keeps_watched_lots_within_keep_window(
    _patched_get_session: AsyncSession,
) -> None:
    """INTERESTED lot closed 30 days ago (within KEEP_NOTIFIED_DAYS) is retained."""
    session = _patched_get_session
    # 30 days > DISTILL_AGE_DAYS (14) but < KEEP_NOTIFIED_DAYS (90) → kept
    closed_30_days_ago = _NOW - timedelta(days=30)
    auction = _make_auction(session)
    _make_lot(
        session,
        auction,
        closed_at=closed_30_days_ago,
        user_action=UserAction.INTERESTED,
    )
    await session.flush()

    await main(now=_NOW)

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 0


async def test_main_keeps_maybe_lots_within_keep_window(
    _patched_get_session: AsyncSession,
) -> None:
    """MAYBE lot closed 30 days ago is also retained within KEEP_NOTIFIED_DAYS."""
    session = _patched_get_session
    closed_30_days_ago = _NOW - timedelta(days=30)
    auction = _make_auction(session)
    _make_lot(
        session,
        auction,
        closed_at=closed_30_days_ago,
        user_action=UserAction.MAYBE,
    )
    await session.flush()

    await main(now=_NOW)

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 0


async def test_main_distills_old_watched_lots(
    _patched_get_session: AsyncSession,
) -> None:
    """INTERESTED lot closed 100 days ago (past KEEP_NOTIFIED_DAYS) is distilled."""
    session = _patched_get_session
    closed_100_days_ago = _NOW - timedelta(days=100)
    auction = _make_auction(session)
    _make_lot(
        session,
        auction,
        closed_at=closed_100_days_ago,
        user_action=UserAction.INTERESTED,
    )
    await session.flush()

    await main(now=_NOW)

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 1


async def test_main_distills_eligible_lot_end_to_end(
    _patched_get_session: AsyncSession,
) -> None:
    """Full main() flow: old closed lot removed from auction_lots, present in historical_sales."""
    session = _patched_get_session
    auction = _make_auction(session)
    lot = _make_lot(session, auction, final_bid_cad=Decimal("6000.00"))
    await session.flush()
    lot_id = lot.id

    await main(now=_NOW)

    # Lot row gone.
    remaining_lot = (
        await session.execute(sa_select(AuctionLot).where(AuctionLot.id == lot_id))
    ).scalar_one_or_none()
    assert remaining_lot is None

    # Historical sale present with correct fields.
    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 1
    assert sales[0].final_listed_price_cad == Decimal("6000.00")
    assert sales[0].sale_channel == "auction_estate"
    assert sales[0].disposition_reason == "sold"


async def test_main_distills_sold_and_unsold_status(
    _patched_get_session: AsyncSession,
) -> None:
    """Both SOLD and UNSOLD lot_status variants are eligible."""
    session = _patched_get_session
    auction = _make_auction(session)
    _make_lot(session, auction, source_lot_id="L1", lot_status=LotStatus.SOLD)
    _make_lot(session, auction, source_lot_id="L2", lot_status=LotStatus.UNSOLD)
    await session.flush()

    await main(now=_NOW)

    sales = (await session.execute(sa_select(HistoricalSale))).scalars().all()
    assert len(sales) == 2  # noqa: PLR2004


async def test_main_bad_lot_does_not_block_others(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One distill failure does not prevent other lots from being distilled."""
    session = _patched_get_session
    auction = _make_auction(session)
    good_lot = _make_lot(session, auction, source_lot_id="LG")
    bad_lot = _make_lot(session, auction, source_lot_id="LB")
    await session.flush()
    good_id = good_lot.id
    bad_id = bad_lot.id

    # Inject a failure for exactly one lot id.
    original = distiller_mod.distill_lot

    async def patched_distill(
        s: AsyncSession,
        lot: AuctionLot,
        auc: Auction,
    ) -> None:
        if lot.id == bad_id:
            raise RuntimeError("injected failure")
        return await original(s, lot, auc)

    monkeypatch.setattr(distiller_mod, "distill_lot", patched_distill)

    await main(now=_NOW)

    # Good lot was distilled.
    remaining_good = (
        await session.execute(sa_select(AuctionLot).where(AuctionLot.id == good_id))
    ).scalar_one_or_none()
    assert remaining_good is None

    # Bad lot still in auction_lots (its per-lot transaction was rolled back).
    remaining_bad = (
        await session.execute(sa_select(AuctionLot).where(AuctionLot.id == bad_id))
    ).scalar_one_or_none()
    assert remaining_bad is not None
