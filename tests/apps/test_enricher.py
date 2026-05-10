from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import APIError, RateLimitError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.enricher import enricher as enricher_mod
from carbuyer.apps.enricher.enricher import (
    SPARSE_LISTING_CONFIDENCE_THRESHOLD,
    _apply_to_lot,
    _classify_failure,
    _EnrichmentResult,
    _process_one,
)
from carbuyer.db.enums import EnrichmentStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.llm.schemas import (
    CarfaxFindings,
    EnrichmentOutput,
    FlagInstance,
    NormalizedVehicle,
    RarityAssessment,
)


def _enrichment(
    *,
    condition: str = "good",
    confidence: float = 0.7,
    transmission: str = "automatic",
    drivetrain: str = "4wd",
    description_quality: str = "adequate",
    red_flags: list[FlagInstance] | None = None,
    desirable: bool = False,
    classic: bool = False,
    summary: str = "ok",
    carfax_url: str | None = None,
) -> EnrichmentOutput:
    return EnrichmentOutput(
        normalized_vehicle=NormalizedVehicle(
            year=2010, make="Ford", model="F-150", trim=None,
            engine="5.4L", transmission=transmission,  # type: ignore[arg-type]
            drivetrain=drivetrain,  # type: ignore[arg-type]
            mileage_km=200000, vin=None,
        ),
        title_status="NORMAL",
        condition_categorical=condition,  # type: ignore[arg-type]
        condition_confidence=confidence,
        red_flags=red_flags or [],
        green_flags=[],
        showstopper_flags=[],
        carfax_url=carfax_url,
        summary=summary,
        description_quality=description_quality,  # type: ignore[arg-type]
        rarity=RarityAssessment(
            desirable_trim_or_spec=desirable,
            classic_or_collector=classic,
            desirability_signals=[], desirability_evidence=[],
        ),
    )


def _seed_auction_and_lot(
    session: AsyncSession,
    *,
    title: str = "2010 Ford F150",
    description: str = "runs and drives, see carfax",
) -> tuple[Auction, AuctionLot]:
    a = Auction(
        source="test", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        scheduled_end_at=datetime(2026, 6, 1, tzinfo=UTC),
        pickup_province="AB",
    )
    session.add(a)
    return a, AuctionLot(
        auction=a, source_lot_id="L1", url="https://x/lot/1",
        title=title, description=description,
        photos=["https://x/p1.jpg"],
    )


# ─── _apply_to_lot ───


@pytest.mark.asyncio
async def test_apply_to_lot_writes_all_enrichment_fields(
    session: AsyncSession,
) -> None:
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()

    result = _EnrichmentResult(
        output=_enrichment(
            red_flags=[FlagInstance(
                flag="needs_work", evidence="needs work", weight=-1,
            )],
            desirable=False, classic=False,
        ),
        carfax_findings=None,
    )
    _apply_to_lot(lot, result, raw_carfax_url="https://www.carfax.ca/vhr/abc")
    await session.flush()

    assert lot.enrichment_status == EnrichmentStatus.DONE
    assert lot.valuation_status == ValuationStatus.PENDING
    assert lot.condition_categorical == "good"
    assert lot.description_quality == "adequate"
    assert lot.title_status == "NORMAL"
    assert lot.condition_inferred_from_sparse_listing is False
    assert len(lot.red_flags) == 1
    assert lot.carfax_url == "https://www.carfax.ca/vhr/abc"
    assert lot.enrichment_version == "v1"


@pytest.mark.asyncio
async def test_apply_to_lot_clamps_low_confidence_to_decent_with_flag(
    session: AsyncSession,
) -> None:
    """Phase 3 design overlay #14/#15: code-side clamp + sparse-listing flag,
    not prompt-side. Phase 4 valuation distinguishes 'actually decent' from
    'we don't know' via this boolean."""
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()

    low_conf = SPARSE_LISTING_CONFIDENCE_THRESHOLD - 0.1
    result = _EnrichmentResult(
        output=_enrichment(condition="great", confidence=low_conf),
        carfax_findings=None,
    )
    _apply_to_lot(lot, result, raw_carfax_url=None)
    assert lot.condition_categorical == "decent"
    assert lot.condition_inferred_from_sparse_listing is True


@pytest.mark.asyncio
async def test_apply_to_lot_preserves_unknown_transmission_drivetrain(
    session: AsyncSession,
) -> None:
    """Phase 3 design overlay #5: re-enrichment must not regress prior
    non-unknown transmission/drivetrain to 'unknown'."""
    _, lot = _seed_auction_and_lot(session)
    lot.transmission = "automatic"
    lot.drivetrain = "4wd"
    session.add(lot)
    await session.flush()

    result = _EnrichmentResult(
        output=_enrichment(transmission="unknown", drivetrain="unknown"),
        carfax_findings=None,
    )
    _apply_to_lot(lot, result, raw_carfax_url=None)
    assert lot.transmission == "automatic"  # preserved
    assert lot.drivetrain == "4wd"  # preserved


@pytest.mark.asyncio
async def test_apply_to_lot_attaches_carfax_findings(
    session: AsyncSession,
) -> None:
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()

    findings = CarfaxFindings(
        accident_count=2, accident_severity_max="moderate",
        service_record_density="regular", ownership_count=2,
        title_brands=[], odometer_consistency="consistent",
    )
    result = _EnrichmentResult(output=_enrichment(), carfax_findings=findings)
    _apply_to_lot(lot, result, raw_carfax_url=None)
    assert lot.carfax_findings is not None
    assert lot.carfax_findings["accident_count"] == 2  # noqa: PLR2004


# ─── _classify_failure ───


def test_classify_failure_rate_limit_is_transient() -> None:
    fake = MagicMock(spec=RateLimitError)
    fake.__class__ = RateLimitError
    assert _classify_failure(RateLimitError.__new__(RateLimitError)) == "transient"


def test_classify_failure_api_error_is_transient() -> None:
    assert _classify_failure(APIError.__new__(APIError)) == "transient"


def test_classify_failure_validation_is_permanent() -> None:
    try:
        EnrichmentOutput.model_validate({"junk": True})
    except ValidationError as exc:
        assert _classify_failure(exc) == "permanent"


def test_classify_failure_unknown_is_transient() -> None:
    assert _classify_failure(RuntimeError("unexpected")) == "transient"


# ─── _process_one round trip ───
# These tests exercise enricher worker logic against the test DB. Because the
# session fixture uses savepoint isolation, get_session() opening a fresh
# connection cannot see uncommitted savepoint data — so we patch get_session
# to yield the test session itself. The session.begin() blocks inside
# _process_one then become nested savepoints under the existing transaction.


@pytest.fixture
def _patched_get_session(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    """Patch enricher's `get_session` to spawn a *new* nested session against
    the test connection on every call. The outer transaction remains the test's
    rolled-back-on-teardown txn; each fresh session creates its own savepoint.
    """
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(enricher_mod, "get_session", fake_get_session)
    return session


@pytest.mark.asyncio
async def test_process_one_success_marks_done_and_pending_valuation(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()

    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"
    provider.describe = AsyncMock(return_value=_enrichment())

    ok = await _process_one(lot.id, provider=provider)
    assert ok is True
    await session.refresh(lot)
    assert lot.enrichment_status == EnrichmentStatus.DONE
    assert lot.valuation_status == ValuationStatus.PENDING
    assert lot.enrichment_attempts == 1
    assert lot.last_enrichment_error is None


@pytest.mark.asyncio
async def test_process_one_transient_failure_keeps_pending_until_max(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 design overlay #4 + #26."""
    monkeypatch.setattr(
        "carbuyer.apps.enricher.enricher.settings.enrichment_max_attempts", 3,
    )
    session = _patched_get_session
    _, lot = _seed_auction_and_lot(session)
    lot.enrichment_status = EnrichmentStatus.IN_PROGRESS
    session.add(lot)
    await session.flush()

    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"
    provider.describe = AsyncMock(side_effect=RuntimeError("boom"))

    # Attempt 1 — transient, leaves PENDING.
    await _process_one(lot.id, provider=provider)
    await session.refresh(lot)
    assert lot.enrichment_status == EnrichmentStatus.PENDING
    assert lot.enrichment_attempts == 1

    # Attempts 2 + 3 — final attempt flips to FAILED.
    await _process_one(lot.id, provider=provider)
    await session.refresh(lot)
    assert lot.enrichment_attempts == 2  # noqa: PLR2004
    assert lot.enrichment_status == EnrichmentStatus.PENDING

    await _process_one(lot.id, provider=provider)
    await session.refresh(lot)
    assert lot.enrichment_attempts == 3  # noqa: PLR2004
    assert lot.enrichment_status == EnrichmentStatus.FAILED
    assert lot.last_enrichment_error is not None
    assert "boom" in lot.last_enrichment_error


@pytest.mark.asyncio
async def test_process_one_permanent_failure_fails_immediately(
    _patched_get_session: AsyncSession,
) -> None:
    """ValidationError fails at attempts=1 — re-running won't change LLM output."""
    session = _patched_get_session
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()

    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"

    def _raise() -> None:
        EnrichmentOutput.model_validate({"junk": True})

    async def _describe(_payload: object) -> object:
        _raise()  # always raises
        return None

    provider.describe = AsyncMock(side_effect=_describe)

    await _process_one(lot.id, provider=provider)
    await session.refresh(lot)
    assert lot.enrichment_attempts == 1
    assert lot.enrichment_status == EnrichmentStatus.FAILED


@pytest.mark.asyncio
async def test_process_one_emits_valuation_pending_notify(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notify is what triggers Phase 4 valuator. Verify it fires on
    success — the actual NOTIFY -> LISTEN round trip is covered by the
    notify module's tests; here we assert the call was made."""
    session = _patched_get_session
    notified: list[tuple[str, str]] = []

    async def fake_notify(_session: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()

    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"
    provider.describe = AsyncMock(return_value=_enrichment())

    monkeypatch.setattr(enricher_mod, "notify", fake_notify)
    await _process_one(lot.id, provider=provider)

    assert ("valuation_pending", str(lot.id)) in notified
