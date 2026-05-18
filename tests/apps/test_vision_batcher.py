"""Tests for vision_batcher.batcher — _bucket_diff, _select_shortlist, _process_one.

Uses the _patched_get_session fixture pattern from test_enricher.py /
test_bid_poller.py: patches get_session on the batcher module so sessions
opened inside _process_one share the test's outer rolled-back transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.vision_batcher import batcher as batcher_mod
from carbuyer.apps.vision_batcher.batcher import (  # pyright: ignore[reportPrivateUsage]
    _bucket_diff,
    _process_one,
    _select_shortlist,
    main,
)
from carbuyer.db.enums import ValuationStatus, VisionStatus
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.llm.schemas import VisionOutput

# ── factories ────────────────────────────────────────────────────────────────


def _make_vision_output(
    *,
    overall: Literal["bad", "poor", "decent", "good", "great"] = "good",
    confidence: float = 0.5,
    contradictions: list[str] | None = None,
) -> VisionOutput:
    return VisionOutput(
        coverage_gaps=[],
        cross_panel_paint_consistency="consistent",
        staging_signals=[],
        overall_red_flags=[],
        overall_green_flags=[],
        exterior_condition=overall,
        interior_condition=overall,
        overall_vision_condition=overall,
        vision_confidence=confidence,
        contradictions_with_description=contradictions or [],
    )


def _seed_lot(
    session: AsyncSession,
    *,
    vision_status: str = "pending",
    lot_status: str = "open",
    price_deal_score: float | None = 0.20,
    condition_categorical: str | None = "good",
    photos: list[str] | None = None,
) -> tuple[Auction, AuctionLot]:
    a = Auction(
        source="test",
        source_auction_id="A1",
        url="https://x",
        canonical_url="https://x",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        scheduled_end_at=datetime(2026, 6, 1, tzinfo=UTC),
        pickup_province="AB",
    )
    session.add(a)
    lot = AuctionLot(
        auction=a,
        source_lot_id="L1",
        url="https://x/lot/1",
        title="2010 Toyota Tundra",
        description="runs fine",
        vision_status=vision_status,
        lot_status=lot_status,
        price_deal_score=price_deal_score,
        condition_categorical=condition_categorical,
        photos=photos or ["https://x/p1.jpg"],
    )
    return a, lot


# ── fixture ──────────────────────────────────────────────────────────────────


@pytest.fixture
def _patched_get_session(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    """Patch batcher's get_session to use the test connection."""
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(batcher_mod, "get_session", fake_get_session)
    return session


# ── _bucket_diff ──────────────────────────────────────────────────────────────


def test_bucket_diff_returns_zero_if_first_is_none() -> None:
    assert _bucket_diff(None, "good") == 0


def test_bucket_diff_returns_zero_if_second_is_none() -> None:
    assert _bucket_diff("good", None) == 0


def test_bucket_diff_returns_zero_if_both_none() -> None:
    assert _bucket_diff(None, None) == 0


def test_bucket_diff_same_bucket_is_zero() -> None:
    assert _bucket_diff("good", "good") == 0


def test_bucket_diff_adjacent_buckets() -> None:
    assert _bucket_diff("good", "decent") == 1


def test_bucket_diff_two_apart() -> None:
    assert _bucket_diff("great", "decent") == 2  # noqa: PLR2004


def test_bucket_diff_extremes() -> None:
    assert _bucket_diff("bad", "great") == 4  # noqa: PLR2004


def test_bucket_diff_is_absolute() -> None:
    # Order shouldn't matter.
    assert _bucket_diff("poor", "great") == _bucket_diff("great", "poor")


def test_bucket_diff_unknown_string_treated_as_rank_2() -> None:
    # Both unknown → both resolve to 2, diff = 0.
    assert _bucket_diff("exotic", "mystery") == 0


# ── _select_shortlist ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_select_shortlist_filters_pending_open_above_threshold(
    session: AsyncSession,
) -> None:
    """Only pending, open, above-threshold lots are returned."""
    a = Auction(
        source="test",
        source_auction_id="A1",
        url="https://x",
        canonical_url="https://x",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        scheduled_end_at=datetime(2026, 6, 1, tzinfo=UTC),
        pickup_province="AB",
    )
    session.add(a)

    # Should be selected.
    l1 = AuctionLot(
        auction=a,
        source_lot_id="L1",
        url="https://x/lot/1",
        title="keep",
        description="x",
        vision_status="pending",
        lot_status="open",
        price_deal_score=0.25,
    )
    # Filtered: done status.
    l2 = AuctionLot(
        auction=a,
        source_lot_id="L2",
        url="https://x/lot/2",
        title="skip-done",
        description="x",
        vision_status="done",
        lot_status="open",
        price_deal_score=0.25,
    )
    # Filtered: closed lot.
    l3 = AuctionLot(
        auction=a,
        source_lot_id="L3",
        url="https://x/lot/3",
        title="skip-closed",
        description="x",
        vision_status="pending",
        lot_status="closed",
        price_deal_score=0.25,
    )
    # Filtered: score below threshold.
    l4 = AuctionLot(
        auction=a,
        source_lot_id="L4",
        url="https://x/lot/4",
        title="skip-low-score",
        description="x",
        vision_status="pending",
        lot_status="open",
        price_deal_score=0.05,
    )
    # Should also be selected (closing_soon).
    l5 = AuctionLot(
        auction=a,
        source_lot_id="L5",
        url="https://x/lot/5",
        title="keep-closing-soon",
        description="x",
        vision_status="pending",
        lot_status="closing_soon",
        price_deal_score=0.30,
    )
    session.add_all([l1, l2, l3, l4, l5])
    await session.flush()

    ids = await _select_shortlist(session, threshold=0.10, limit=100)

    assert l1.id in ids
    assert l5.id in ids
    assert l2.id not in ids
    assert l3.id not in ids
    assert l4.id not in ids


@pytest.mark.asyncio
async def test_select_shortlist_ordered_by_score_desc(
    session: AsyncSession,
) -> None:
    """Lots returned in descending price_deal_score order."""
    a = Auction(
        source="test",
        source_auction_id="A2",
        url="https://y",
        canonical_url="https://y",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        scheduled_end_at=datetime(2026, 6, 1, tzinfo=UTC),
        pickup_province="AB",
    )
    session.add(a)

    scores = [0.15, 0.35, 0.20, 0.50]
    lots = [
        AuctionLot(
            auction=a,
            source_lot_id=f"L{i}",
            url=f"https://y/lot/{i}",
            title=f"lot{i}",
            description="x",
            vision_status="pending",
            lot_status="open",
            price_deal_score=s,
        )
        for i, s in enumerate(scores)
    ]
    session.add_all(lots)
    await session.flush()

    ids = await _select_shortlist(session, threshold=0.10, limit=100)
    # Extract just the scores we inserted by matching ids. price_deal_score is
    # float | None on the model but all seeded values are non-None floats.
    id_to_score = {lot.id: float(lot.price_deal_score or 0.0) for lot in lots}
    returned_scores = [id_to_score[i] for i in ids if i in id_to_score]
    assert returned_scores == sorted(returned_scores, reverse=True)


@pytest.mark.asyncio
async def test_select_shortlist_respects_limit(
    session: AsyncSession,
) -> None:
    a = Auction(
        source="test",
        source_auction_id="A3",
        url="https://z",
        canonical_url="https://z",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        scheduled_end_at=datetime(2026, 6, 1, tzinfo=UTC),
        pickup_province="AB",
    )
    session.add(a)

    lots = [
        AuctionLot(
            auction=a,
            source_lot_id=f"Lim{i}",
            url=f"https://z/lot/{i}",
            title=f"lot{i}",
            description="x",
            vision_status="pending",
            lot_status="open",
            price_deal_score=0.20,
        )
        for i in range(5)
    ]
    session.add_all(lots)
    await session.flush()

    ids = await _select_shortlist(session, threshold=0.10, limit=3)
    assert len(ids) <= 3  # noqa: PLR2004


# ── _process_one ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_one_no_photos_returns_skipped(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lot with empty photos list → SKIPPED, no LLM call."""
    session = _patched_get_session
    _, lot = _seed_lot(session, photos=[])
    session.add(lot)
    await session.flush()

    provider = MagicMock()
    provider.vision = AsyncMock()

    outcome = await _process_one(lot.id, provider=provider)
    assert outcome == "skipped"
    await session.refresh(lot)
    assert lot.vision_status == VisionStatus.SKIPPED
    provider.vision.assert_not_called()


@pytest.mark.asyncio
async def test_process_one_download_returns_empty_returns_skipped(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All photos fail to download → SKIPPED, no LLM call."""
    session = _patched_get_session
    _, lot = _seed_lot(session, photos=["https://fail.example/p1.jpg"])
    session.add(lot)
    await session.flush()

    # Make download_and_resize always return empty (all downloads failed).
    monkeypatch.setattr(batcher_mod, "download_and_resize", AsyncMock(return_value=[]))

    provider = MagicMock()
    provider.vision = AsyncMock()

    outcome = await _process_one(lot.id, provider=provider)
    assert outcome == "skipped"
    await session.refresh(lot)
    assert lot.vision_status == VisionStatus.SKIPPED
    provider.vision.assert_not_called()


@pytest.mark.asyncio
async def test_process_one_vision_raises_returns_failed(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider.vision raises → FAILED status, no NOTIFY."""
    session = _patched_get_session
    _, lot = _seed_lot(session)
    session.add(lot)
    await session.flush()

    monkeypatch.setattr(
        batcher_mod,
        "download_and_resize",
        AsyncMock(return_value=["/tmp/fake.jpg"]),
    )

    provider = MagicMock()
    provider.vision = AsyncMock(side_effect=RuntimeError("LLM down"))

    notified: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(batcher_mod, "notify", fake_notify)

    outcome = await _process_one(lot.id, provider=provider)
    assert outcome == "failed"
    await session.refresh(lot)
    assert lot.vision_status == VisionStatus.FAILED
    assert notified == []


@pytest.mark.asyncio
async def test_process_one_happy_path_no_pessimistic_update(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success path: vision confidence too low for pessimistic override.

    vision_findings written, vision_status=DONE, valuation_status unchanged,
    no NOTIFY emitted.
    """
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        condition_categorical="good",
        photos=["https://x/p1.jpg"],
    )
    lot.valuation_status = ValuationStatus.DONE
    session.add(lot)
    await session.flush()

    # Vision says "poor" but confidence is 0.5 — below _PESSIMISM_CONFIDENCE_THRESHOLD.
    out = _make_vision_output(overall="poor", confidence=0.5, contradictions=["paint mismatch"])

    monkeypatch.setattr(
        batcher_mod,
        "download_and_resize",
        AsyncMock(return_value=["/tmp/fake.jpg"]),
    )
    provider = MagicMock()
    provider.vision = AsyncMock(return_value=out)

    notified: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(batcher_mod, "notify", fake_notify)

    outcome = await _process_one(lot.id, provider=provider)
    assert outcome == "done"
    await session.refresh(lot)
    assert lot.vision_status == VisionStatus.DONE
    assert lot.vision_condition_overall == "poor"
    assert lot.vision_confidence == 0.5  # noqa: PLR2004
    assert lot.vision_contradictions == ["paint mismatch"]
    # Pessimistic override must NOT fire — confidence too low.
    assert lot.condition_categorical == "good"
    assert lot.valuation_status == ValuationStatus.DONE
    assert notified == []


@pytest.mark.asyncio
async def test_process_one_pessimistic_update_high_confidence_large_diff(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pessimistic override fires when confidence > 0.7 and bucket diff >= 2.

    Description says "good", vision says "bad" (diff=3, confidence=0.85):
    - condition_categorical revised to "bad"
    - synthetic red flag appended
    - valuation_status = PENDING
    - NOTIFY valuation_pending emitted
    """
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        condition_categorical="good",
        photos=["https://x/p1.jpg"],
    )
    lot.valuation_status = ValuationStatus.DONE
    session.add(lot)
    await session.flush()

    out = _make_vision_output(
        overall="bad",
        confidence=0.85,
        contradictions=["severe rust", "cracked bumper"],
    )

    monkeypatch.setattr(
        batcher_mod,
        "download_and_resize",
        AsyncMock(return_value=["/tmp/fake.jpg"]),
    )
    provider = MagicMock()
    provider.vision = AsyncMock(return_value=out)

    notified: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(batcher_mod, "notify", fake_notify)

    outcome = await _process_one(lot.id, provider=provider)
    assert outcome == "done"
    await session.refresh(lot)

    assert lot.vision_status == VisionStatus.DONE
    assert lot.condition_categorical == "bad"
    assert lot.valuation_status == ValuationStatus.PENDING

    # Synthetic red flag appended.
    flag_names = [f["flag"] for f in lot.red_flags]
    assert "description_oversells_condition" in flag_names

    assert ("valuation_pending", str(lot.id)) in notified


@pytest.mark.asyncio
async def test_process_one_no_override_when_vision_sees_better_than_description(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 8 overlay #23 / Phase 13 fix: bucket-diff is absolute, override
    must be one-sided. Vision seeing the lot as BETTER than the description
    (e.g. desc=poor, vision=great, confidence=0.85) must NOT flip
    condition_categorical down, NOT fire description_oversells_condition, NOT
    reset valuation_status, NOT emit NOTIFY.
    """
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        condition_categorical="poor",
        photos=["https://x/p1.jpg"],
    )
    lot.valuation_status = ValuationStatus.DONE
    session.add(lot)
    await session.flush()

    # bucket-diff = 3, confidence above threshold — but vision_rank > desc_rank.
    out = _make_vision_output(overall="great", confidence=0.85)

    monkeypatch.setattr(
        batcher_mod,
        "download_and_resize",
        AsyncMock(return_value=["/tmp/fake.jpg"]),
    )
    provider = MagicMock()
    provider.vision = AsyncMock(return_value=out)

    notified: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(batcher_mod, "notify", fake_notify)

    outcome = await _process_one(lot.id, provider=provider)
    assert outcome == "done"
    await session.refresh(lot)

    # No downward flip; no synthetic flag; no rescore trigger.
    assert lot.condition_categorical == "poor"
    assert lot.valuation_status == ValuationStatus.DONE
    flag_names = [f["flag"] for f in (lot.red_flags or [])]
    assert "description_oversells_condition" not in flag_names
    assert notified == []


@pytest.mark.asyncio
async def test_process_one_pessimistic_override_is_idempotent(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A content rescrape can reset vision_status=PENDING, re-running vision on
    a lot that already has description_oversells_condition. The synthetic flag
    must not duplicate; red_flags would otherwise accumulate copies."""
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        condition_categorical="good",
        photos=["https://x/p1.jpg"],
    )
    # Lot already carries the synthetic flag from a prior nightly run.
    lot.red_flags = [
        {"flag": "description_oversells_condition", "evidence": "old", "weight": -2}
    ]
    lot.valuation_status = ValuationStatus.DONE
    session.add(lot)
    await session.flush()

    out = _make_vision_output(
        overall="bad", confidence=0.85, contradictions=["new evidence"],
    )

    monkeypatch.setattr(
        batcher_mod,
        "download_and_resize",
        AsyncMock(return_value=["/tmp/fake.jpg"]),
    )
    provider = MagicMock()
    provider.vision = AsyncMock(return_value=out)
    monkeypatch.setattr(batcher_mod, "notify", AsyncMock())

    await _process_one(lot.id, provider=provider)
    await session.refresh(lot)

    flag_names = [f["flag"] for f in lot.red_flags]
    assert flag_names.count("description_oversells_condition") == 1


@pytest.mark.asyncio
async def test_process_one_no_pessimistic_update_when_diff_too_small(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High confidence but only 1 bucket diff — override must NOT fire."""
    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        condition_categorical="good",
        photos=["https://x/p1.jpg"],
    )
    lot.valuation_status = ValuationStatus.DONE
    session.add(lot)
    await session.flush()

    # Vision says "decent" (diff=1 from "good") — below _PESSIMISM_BUCKET_DIFF_MIN.
    out = _make_vision_output(overall="decent", confidence=0.90)

    monkeypatch.setattr(
        batcher_mod,
        "download_and_resize",
        AsyncMock(return_value=["/tmp/fake.jpg"]),
    )
    provider = MagicMock()
    provider.vision = AsyncMock(return_value=out)

    notified: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(batcher_mod, "notify", fake_notify)

    await _process_one(lot.id, provider=provider)
    await session.refresh(lot)

    assert lot.condition_categorical == "good"  # unchanged
    assert lot.valuation_status == ValuationStatus.DONE
    assert notified == []


# ── _process_one missing-lot path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_one_missing_lot_returns_missing(
    _patched_get_session: AsyncSession,
) -> None:
    """Read-tx finds no row for the given id → 'missing', no LLM call.

    Read-tx case (the simpler and more likely race window: the lot was deleted
    between shortlist selection and per-lot processing). Provider is a strict
    MagicMock — any attempt to call .vision() would raise.
    """
    nonexistent_lot_id = 999_999_999
    provider = MagicMock()
    provider.vision = AsyncMock(side_effect=AssertionError("vision must not be called"))

    outcome = await _process_one(nonexistent_lot_id, provider=provider)
    assert outcome == "missing"
    provider.vision.assert_not_called()


# ── main() fail-fast ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_main_exits_when_openai_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "carbuyer.apps.vision_batcher.batcher.settings.openai_api_key",
        "",
    )
    with pytest.raises(SystemExit, match="OPENAI_API_KEY not configured"):
        await main()
