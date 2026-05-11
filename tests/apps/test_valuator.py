"""Phase 4 valuator worker tests.

Mirrors the enricher test pattern: ``_patched_get_session`` makes the worker's
``get_session()`` / ``get_session_maker()`` calls reuse the test connection,
so per-lot transactions are nested savepoints under the test's outer
rolled-back transaction.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.valuator import valuator as valuator_mod
from carbuyer.apps.valuator.valuator import (
    SUSPICIOUS_UNDERPRICE_FRACTION,
    _process_one,
    process_pending,
    value_one,
)
from carbuyer.db.enums import NotificationStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale
from carbuyer.scoring.fair_value import ConfidenceBucket


def _seed_auction(session: AsyncSession, **overrides: object) -> Auction:
    base: dict[str, object] = dict(
        source="test", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        pickup_province="AB",
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
    )
    base.update(overrides)
    a = Auction(**base)  # type: ignore[arg-type]
    session.add(a)
    return a


def _seed_lot(
    session: AsyncSession,
    auction: Auction,
    *,
    source_lot_id: str = "L1",
    make: str | None = "Toyota",
    model: str | None = "Tacoma",
    year: int | None = 2015,
    mileage_km: int | None = 150000,
    condition: str | None = "decent",
    sparse: bool = False,
    description_quality: str | None = "adequate",
    red_flags: list[dict[str, Any]] | None = None,
    green_flags: list[dict[str, Any]] | None = None,
    showstopper_flags: list[dict[str, Any]] | None = None,
    current_high_bid: Decimal | None = Decimal("12000"),
    desirable_trim_or_spec: bool = False,
    classic_or_collector: bool = False,
) -> AuctionLot:
    lot = AuctionLot(
        auction_id=auction.id, source_lot_id=source_lot_id,
        url=f"https://x/lot/{source_lot_id}",
        title=f"{year} {make} {model}",
        description="A " + ("real description. " * 30),
        year=year, make=make, model=model, mileage_km=mileage_km,
        condition_categorical=condition,
        condition_inferred_from_sparse_listing=sparse,
        description_quality=description_quality,
        red_flags=red_flags or [],
        green_flags=green_flags or [],
        showstopper_flags=showstopper_flags or [],
        current_high_bid_cad=current_high_bid,
        desirable_trim_or_spec=desirable_trim_or_spec,
        classic_or_collector=classic_or_collector,
    )
    session.add(lot)
    return lot


def _seed_comps(session: AsyncSession, prices: list[int]) -> None:
    for p in prices:
        session.add(HistoricalSale(
            year=2015, mileage_km=150000,
            make="Toyota", model="Tacoma", trim=None,
            sale_channel="auction_estate", sale_platform="hibid",
            title_status="NORMAL", schema_version=1,
            final_listed_price_cad=Decimal(p),
            final_price_with_premium_cad=Decimal(p),
            buyer_premium_pct_at_sale=Decimal("0.10"),
            disposition_reason="sold",
        ))


# ─── value_one ───


@pytest.mark.asyncio
async def test_value_one_writes_full_valuation_when_comps_exist(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    lot = _seed_lot(session, a)
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.valuation_status == ValuationStatus.DONE
    assert lot.notification_status == NotificationStatus.PENDING
    assert lot.confidence_bucket == ConfidenceBucket.HIGH.value
    assert lot.comp_count == 10  # noqa: PLR2004
    assert lot.expected_value_cad is not None
    assert lot.value_low_cad is not None
    assert lot.value_high_cad is not None
    assert lot.price_deal_score is not None
    assert lot.recommended_max_bid_cad is not None
    assert lot.all_in_at_current_bid_cad is not None
    assert lot.landed_cost_premium_cad is not None
    assert lot.scoring_version is not None
    assert lot.weights_hash is not None
    assert lot.flag_score == 0  # no flags fired
    assert lot.historical_comp_count is not None


@pytest.mark.asyncio
async def test_value_one_skipped_when_make_model_year_missing(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = _seed_lot(session, a, make=None, year=None)
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.valuation_status == ValuationStatus.SKIPPED
    # Notification stays at default 'pending' is fine — but the lot was never
    # NOTIFY'd to notification_pending so it'll stay pending and get caught
    # by the next catchup. Actually overlay says: when valuator skips, it
    # should also skip notification so the row terminates cleanly.
    assert lot.notification_status == NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_value_one_marks_insufficient_when_no_comps(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = _seed_lot(session, a)  # no comps seeded
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.valuation_status == ValuationStatus.INSUFFICIENT
    assert lot.confidence_bucket == ConfidenceBucket.INSUFFICIENT.value
    assert lot.expected_value_cad is None
    assert lot.price_deal_score is None
    # No comps means we can't compute a deal — notification is skipped, not
    # spammed with low-confidence guesses.
    assert lot.notification_status == NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_value_one_threads_sparse_to_fair_value(
    session: AsyncSession,
) -> None:
    """Phase 4 overlay #9: condition_inferred_from_sparse_listing must
    actually affect the expected value, otherwise the dual write is dead."""
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    confident = _seed_lot(session, a, source_lot_id="confident", sparse=False)
    sparse = _seed_lot(session, a, source_lot_id="sparse", sparse=True)
    await session.commit()

    await value_one(session, confident)
    await value_one(session, sparse)
    await session.commit()

    assert confident.expected_value_cad is not None
    assert sparse.expected_value_cad is not None
    assert sparse.expected_value_cad < confident.expected_value_cad


@pytest.mark.asyncio
async def test_value_one_threads_description_quality_to_flag_score(
    session: AsyncSession,
) -> None:
    """Phase 4 overlay #10: thin descriptions floor flag_score at -2 even
    when the cumulative red weight would otherwise be lower."""
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    heavy_reds = [
        {"flag": "engine_knock", "weight": -3, "evidence": "x"},
        {"flag": "transmission_slipping", "weight": -3, "evidence": "x"},
    ]
    thin = _seed_lot(session, a, source_lot_id="thin",
                     description_quality="thin", red_flags=heavy_reds)
    detailed = _seed_lot(session, a, source_lot_id="detailed",
                         description_quality="detailed", red_flags=heavy_reds)
    await session.commit()

    await value_one(session, thin)
    await value_one(session, detailed)
    await session.commit()

    assert thin.flag_score == -2  # noqa: PLR2004 — thin floor
    assert detailed.flag_score == -5  # noqa: PLR2004 — clipped


@pytest.mark.asyncio
async def test_value_one_skips_notification_on_showstopper(
    session: AsyncSession,
) -> None:
    """Phase 4 overlay #12 (paragraph 1): showstoppers always skip
    notification, regardless of price-deal score."""
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    lot = _seed_lot(session, a, showstopper_flags=[
        {"flag": "engine_seized", "evidence": "won't turn"},
    ])
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.valuation_status == ValuationStatus.DONE
    assert lot.notification_status == NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_value_one_skips_notification_on_excessive_red_weight(
    session: AsyncSession,
) -> None:
    """Phase 4 overlay #12: cumulative raw weight at or below the threshold
    triggers a notification skip even without any showstopper."""
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    # Three -3 reds = -9, below the -8 default threshold.
    heavy_reds = [
        {"flag": "engine_knock", "weight": -3, "evidence": "x"},
        {"flag": "transmission_slipping", "weight": -3, "evidence": "x"},
        {"flag": "frame_rust", "weight": -3, "evidence": "x"},
    ]
    lot = _seed_lot(session, a, red_flags=heavy_reds)
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.valuation_status == ValuationStatus.DONE
    assert lot.notification_status == NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_value_one_sets_suspicious_underprice_when_far_below_low(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    # current_bid well below 0.85 * value_low (which after channel norm x 1.20
    # will be around 12000-ish; 1000 is "too good to be true").
    lot = _seed_lot(session, a, current_high_bid=Decimal("1000"))
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.suspicious_underprice_flag is True
    assert lot.value_low_cad is not None
    assert SUSPICIOUS_UNDERPRICE_FRACTION < 1


@pytest.mark.asyncio
async def test_value_one_no_current_bid_skips_price_deal_score(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    lot = _seed_lot(session, a, current_high_bid=None)
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.valuation_status == ValuationStatus.DONE
    assert lot.expected_value_cad is not None
    # Without a current bid we can't compute a deal score.
    assert lot.price_deal_score is None
    assert lot.all_in_at_current_bid_cad is None


# ─── _process_one + retry counter ───


@pytest.fixture
def _patched_get_session(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    def fake_get_session_maker() -> object:
        return maker

    monkeypatch.setattr(valuator_mod, "get_session", fake_get_session)
    monkeypatch.setattr(valuator_mod, "get_session_maker", fake_get_session_maker)
    return session


@pytest.mark.asyncio
async def test_process_one_success_marks_done_and_notifies(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    notified: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(valuator_mod, "notify", fake_notify)

    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    lot = _seed_lot(session, a)
    await session.flush()

    outcome = await _process_one(lot.id)
    assert outcome == "done"
    await session.refresh(lot)
    assert lot.valuation_status == ValuationStatus.DONE
    assert lot.valuation_attempts == 1
    assert ("notification_pending", str(lot.id)) in notified


@pytest.mark.asyncio
async def test_process_one_transient_failure_keeps_pending_until_max(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "carbuyer.apps.valuator.valuator.settings.valuation_max_attempts", 3,
    )

    # Sabotage compute_fair_value to raise — every value_one() call fails.
    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("comp engine offline")

    monkeypatch.setattr(valuator_mod, "compute_fair_value", boom)

    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    lot = _seed_lot(session, a)
    lot.valuation_status = ValuationStatus.IN_PROGRESS
    await session.flush()

    # Attempt 1: transient.
    await _process_one(lot.id)
    await session.refresh(lot)
    assert lot.valuation_status == ValuationStatus.PENDING
    assert lot.valuation_attempts == 1

    # Attempt 2: transient.
    await _process_one(lot.id)
    await session.refresh(lot)
    assert lot.valuation_attempts == 2  # noqa: PLR2004
    assert lot.valuation_status == ValuationStatus.PENDING

    # Attempt 3: hits max → FAILED.
    await _process_one(lot.id)
    await session.refresh(lot)
    assert lot.valuation_attempts == 3  # noqa: PLR2004
    assert lot.valuation_status == ValuationStatus.FAILED
    assert lot.last_valuation_error is not None
    assert "comp engine offline" in lot.last_valuation_error


# ─── process_pending + self-NOTIFY ───


@pytest.mark.asyncio
async def test_process_pending_claims_and_processes_batch(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    notified: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(valuator_mod, "notify", fake_notify)
    monkeypatch.setattr(
        "carbuyer.apps.valuator.valuator.settings.valuation_batch_size", 10,
    )

    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    lots = [_seed_lot(session, a, source_lot_id=f"L{i}") for i in range(3)]
    await session.flush()
    lot_ids = {lot.id for lot in lots}

    n = await process_pending()
    expected_lot_count = 3
    assert n == expected_lot_count

    payloads = {p for ch, p in notified if ch == "notification_pending"}
    assert {str(i) for i in lot_ids} <= payloads


@pytest.mark.asyncio
async def test_process_pending_self_notifies_on_transient_leftover(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    notified: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(valuator_mod, "notify", fake_notify)
    monkeypatch.setattr(
        "carbuyer.apps.valuator.valuator.settings.valuation_max_attempts", 5,
    )

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("DB blip")

    monkeypatch.setattr(valuator_mod, "compute_fair_value", boom)

    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000, 11000, 12000, 13000, 14000,
                          15000, 16000, 17000, 18000, 19000])
    _seed_lot(session, a)
    await session.flush()

    await process_pending()

    self_notifies = [n for n in notified if n[0] == "valuation_pending"]
    assert len(self_notifies) == 1


@pytest.mark.asyncio
async def test_process_pending_no_pending_returns_zero(
    _patched_get_session: AsyncSession,
) -> None:
    n = await process_pending()
    assert n == 0


# ─── End-to-end with enrichment_status DONE filter ───


@pytest.mark.asyncio
async def test_value_one_recent_auction_lot_comps_contribute(
    session: AsyncSession,
) -> None:
    """Soak-test the comp-set wiring: closed AuctionLots within the recency
    window should contribute as comps without HistoricalSale rows present."""
    a = _seed_auction(session)
    await session.flush()
    # Seed only AuctionLot comps — no HistoricalSale rows.
    for i, p in enumerate([12000, 13000, 14000, 15000, 16000,
                           17000, 18000, 19000, 20000, 21000]):
        session.add(AuctionLot(
            auction_id=a.id, source_lot_id=f"COMP{i}",
            url=f"https://x/comp/{i}",
            title="comp", year=2015, make="Toyota", model="Tacoma",
            mileage_km=150000,
            lot_status="closed",
            closed_at=datetime.now(UTC) - timedelta(days=3),
            final_bid_cad=Decimal(p),
        ))
    target = _seed_lot(session, a, source_lot_id="TARGET")
    await session.commit()

    await value_one(session, target)
    await session.commit()

    assert target.valuation_status == ValuationStatus.DONE
    assert target.comp_count == 10  # noqa: PLR2004
