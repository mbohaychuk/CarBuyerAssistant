from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import (
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.enricher import enricher as enricher_mod
from carbuyer.apps.enricher.enricher import (
    SPARSE_LISTING_CONFIDENCE_THRESHOLD,
    _apply_to_lot,
    _canonical_model,
    _classify_failure,
    _EnrichmentResult,
    _process_one,
    _reliability,
    main,
    process_pending,
)
from carbuyer.db.enums import EnrichmentStatus, NotificationStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot, Search
from carbuyer.llm.schemas import (
    CarfaxFindings,
    EnrichmentOutput,
    FlagInstance,
    NormalizedVehicle,
    RarityAssessment,
)
from carbuyer.wants.criteria import WantCriteria


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
    description: str = (
        "Runs and drives. 280,000 km. Single owner since 2014. "
        "Recent timing chain replacement at 250k. New tires, fresh oil change. "
        "Some surface rust on rocker panels. See carfax in description."
    ),
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
async def test_apply_to_lot_preserves_title_status_on_unknown(
    session: AsyncSession,
) -> None:
    """Phase 3 review: re-enrichment with low confidence must not regress
    a previously confident title_status='NORMAL' to 'UNKNOWN'."""
    _, lot = _seed_auction_and_lot(session)
    lot.title_status = "NORMAL"
    session.add(lot)
    await session.flush()

    out = _enrichment()
    out_with_unknown = out.model_copy(update={"title_status": "UNKNOWN"})
    result = _EnrichmentResult(output=out_with_unknown, carfax_findings=None)
    _apply_to_lot(lot, result, raw_carfax_url=None)
    assert lot.title_status == "NORMAL"  # preserved


@pytest.mark.asyncio
async def test_apply_to_lot_preserves_engine_on_unknown_string(
    session: AsyncSession,
) -> None:
    """The schema has no Literal for `engine`; if the LLM returns the string
    'unknown', we must treat it as 'preserve existing'."""
    _, lot = _seed_auction_and_lot(session)
    lot.engine = "5.4L V8"
    session.add(lot)
    await session.flush()

    out = _enrichment()
    out_unknown_engine = out.model_copy(update={
        "normalized_vehicle": out.normalized_vehicle.model_copy(
            update={"engine": "unknown"},
        ),
    })
    result = _EnrichmentResult(output=out_unknown_engine, carfax_findings=None)
    _apply_to_lot(lot, result, raw_carfax_url=None)
    assert lot.engine == "5.4L V8"


@pytest.mark.asyncio
async def test_apply_to_lot_overrides_description_quality_on_thin(
    session: AsyncSession,
) -> None:
    """Cheap defense against the LLM rating a 50-char listing 'detailed'."""
    _, lot = _seed_auction_and_lot(session, description="too short")
    session.add(lot)
    await session.flush()

    out = _enrichment(description_quality="detailed")
    result = _EnrichmentResult(output=out, carfax_findings=None)
    _apply_to_lot(lot, result, raw_carfax_url=None)
    assert lot.description_quality == "thin"


@pytest.mark.asyncio
async def test_apply_to_lot_rejects_out_of_range_mileage(
    session: AsyncSession,
) -> None:
    """Phase 13: LLM hallucinated mileage (negative, or a mi→km unit swap that
    produces an impossibly large number) must not overwrite the existing value.
    Bad mileage silently poisons the comp set and flips the deal score."""
    _, lot = _seed_auction_and_lot(session)
    lot.mileage_km = 180_000
    session.add(lot)
    await session.flush()

    bad_nv = _enrichment().normalized_vehicle.model_copy(update={"mileage_km": 9_999_999})
    bad = _enrichment().model_copy(update={"normalized_vehicle": bad_nv})
    _apply_to_lot(lot, _EnrichmentResult(output=bad, carfax_findings=None), raw_carfax_url=None)
    assert lot.mileage_km == 180_000


@pytest.mark.asyncio
async def test_apply_to_lot_rejects_negative_mileage(
    session: AsyncSession,
) -> None:
    _, lot = _seed_auction_and_lot(session)
    lot.mileage_km = 150_000
    session.add(lot)
    await session.flush()

    bad_nv = _enrichment().normalized_vehicle.model_copy(update={"mileage_km": -50000})
    bad = _enrichment().model_copy(update={"normalized_vehicle": bad_nv})
    _apply_to_lot(lot, _EnrichmentResult(output=bad, carfax_findings=None), raw_carfax_url=None)
    assert lot.mileage_km == 150_000


@pytest.mark.asyncio
async def test_apply_to_lot_rejects_implausible_year(
    session: AsyncSession,
) -> None:
    """1899 or 2099 — neither is a plausible vehicle year. Preserve prior."""
    _, lot = _seed_auction_and_lot(session)
    lot.year = 2014
    session.add(lot)
    await session.flush()

    bad_nv = _enrichment().normalized_vehicle.model_copy(update={"year": 1899})
    bad = _enrichment().model_copy(update={"normalized_vehicle": bad_nv})
    _apply_to_lot(lot, _EnrichmentResult(output=bad, carfax_findings=None), raw_carfax_url=None)
    assert lot.year == 2014

    bad_nv2 = _enrichment().normalized_vehicle.model_copy(update={"year": 2099})
    bad2 = _enrichment().model_copy(update={"normalized_vehicle": bad_nv2})
    _apply_to_lot(lot, _EnrichmentResult(output=bad2, carfax_findings=None), raw_carfax_url=None)
    assert lot.year == 2014


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
    assert _classify_failure(RateLimitError.__new__(RateLimitError)) == "transient"


def test_classify_failure_validation_is_permanent() -> None:
    try:
        EnrichmentOutput.model_validate({"junk": True})
    except ValidationError as exc:
        assert _classify_failure(exc) == "permanent"


def test_classify_failure_4xx_is_permanent() -> None:
    """Phase 3 review must-fix: AuthenticationError / BadRequestError /
    PermissionDeniedError are 4xx and the SDK does NOT retry them. Treat as
    permanent so a revoked-key incident doesn't burn 3 attempt cycles."""
    response_mock = MagicMock()
    response_mock.status_code = 401
    auth_err = AuthenticationError(
        message="invalid key", response=response_mock, body=None,
    )
    assert _classify_failure(auth_err) == "permanent"

    response_mock.status_code = 400
    bad_req = BadRequestError(
        message="bad request", response=response_mock, body=None,
    )
    assert _classify_failure(bad_req) == "permanent"

    response_mock.status_code = 403
    perm_err = PermissionDeniedError(
        message="forbidden", response=response_mock, body=None,
    )
    assert _classify_failure(perm_err) == "permanent"


def test_classify_failure_5xx_is_transient() -> None:
    response_mock = MagicMock()
    response_mock.status_code = 500
    err = InternalServerError(message="boom", response=response_mock, body=None)
    assert _classify_failure(err) == "transient"


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
    """Patch enricher's `get_session` AND `get_session_maker` to spawn fresh
    sessions against the test connection on every call. The outer transaction
    remains the test's rolled-back-on-teardown txn; each fresh session
    creates its own savepoint.
    """
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    def fake_get_session_maker() -> object:
        return maker

    monkeypatch.setattr(enricher_mod, "get_session", fake_get_session)
    monkeypatch.setattr(enricher_mod, "get_session_maker", fake_get_session_maker)
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

    outcome = await _process_one(lot.id, provider=provider)
    assert outcome == "done"
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


# ─── process_pending round trip ───


@pytest.mark.asyncio
async def test_process_pending_claims_and_processes(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: pending lots → claim_pending_ids → _process_one each →
    NOTIFY. Phase 3 review must-fix #5."""
    session = _patched_get_session
    notified: list[tuple[str, str]] = []

    async def fake_notify(_session: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(enricher_mod, "notify", fake_notify)
    # Sequential claim loop in tests — savepoints can't safely fan out on a
    # single shared connection. In production each coroutine pulls a separate
    # pooled connection, so concurrency > 1 is parallel-safe.
    monkeypatch.setattr(
        "carbuyer.apps.enricher.enricher.settings.openai_concurrency", 1,
    )
    monkeypatch.setattr(
        "carbuyer.apps.enricher.enricher.settings.enrichment_batch_size", 10,
    )

    a = Auction(
        source="test", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()

    expected_lot_count = 3
    lots = [
        AuctionLot(
            auction_id=a.id, source_lot_id=f"L{i}",
            url=f"https://x/lot/{i}", title=f"lot {i}",
            description="x" * 200,  # > THIN_DESCRIPTION_BYTES
        )
        for i in range(expected_lot_count)
    ]
    session.add_all(lots)
    await session.flush()
    lot_ids = {lot.id for lot in lots}

    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"
    provider.describe = AsyncMock(return_value=_enrichment())

    n = await process_pending(provider)
    assert n == expected_lot_count

    # All three lots got the valuation_pending NOTIFY.
    notified_payloads = {payload for ch, payload in notified if ch == "valuation_pending"}
    assert {str(i) for i in lot_ids} <= notified_payloads


@pytest.mark.asyncio
async def test_process_pending_self_notifies_on_transient_leftover(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 review must-fix: when transient failure leaves rows at PENDING,
    self-NOTIFY enrichment_pending so the listener loop drains them without
    waiting for the next worker restart."""
    session = _patched_get_session
    notified: list[tuple[str, str]] = []

    async def fake_notify(_session: object, channel: str, payload: str) -> None:
        notified.append((channel, payload))

    monkeypatch.setattr(enricher_mod, "notify", fake_notify)
    monkeypatch.setattr(
        "carbuyer.apps.enricher.enricher.settings.enrichment_max_attempts", 5,
    )

    a = Auction(
        source="test", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()
    lot = AuctionLot(
        auction_id=a.id, source_lot_id="L1", url="https://x/lot/1",
        title="t", description="x" * 200,
    )
    session.add(lot)
    await session.flush()

    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"
    provider.describe = AsyncMock(side_effect=RuntimeError("transient blip"))

    await process_pending(provider)

    # Self-NOTIFY on enrichment_pending so the listen loop wakes up.
    self_notifies = [n for n in notified if n[0] == "enrichment_pending"]
    assert len(self_notifies) == 1


# ─── main() fail-fast ───


@pytest.mark.asyncio
async def test_main_exits_when_openai_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 design overlay #16: fail at startup, not on first lot."""
    monkeypatch.setattr(
        "carbuyer.apps.enricher.enricher.settings.openai_api_key", "",
    )
    with pytest.raises(SystemExit, match="OPENAI_API_KEY not configured"):
        await main()


# ─── S7: vPIC make/model normalization ───


def _output_with_model(model: str) -> EnrichmentOutput:
    return EnrichmentOutput(
        normalized_vehicle=NormalizedVehicle(
            year=2010, make="Ford", model=model, trim=None, engine="5.4L",
            transmission="automatic", drivetrain="4wd", mileage_km=200000, vin=None,
        ),
        title_status="NORMAL", condition_categorical="good", condition_confidence=0.7,
        red_flags=[], green_flags=[], showstopper_flags=[], carfax_url=None,
        summary="ok", description_quality="adequate",
        rarity=RarityAssessment(
            desirable_trim_or_spec=False, classic_or_collector=False,
            desirability_signals=[], desirability_evidence=[],
        ),
    )


@pytest.mark.asyncio
async def test_canonical_model_noop_when_vpic_disabled() -> None:
    # Off by default → returns the input model and opens no HTTP client.
    assert await _canonical_model("Ford", "F150", 2015) == "F150"


@pytest.mark.asyncio
async def test_apply_to_lot_canonical_model_overrides_llm(session: AsyncSession) -> None:
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()
    result = _EnrichmentResult(output=_output_with_model("F150"), carfax_findings=None)

    _apply_to_lot(lot, result, raw_carfax_url=None, canonical_model="F-150")
    await session.flush()
    assert lot.model == "F-150"


@pytest.mark.asyncio
async def test_apply_to_lot_falls_back_to_llm_model_without_canonical(
    session: AsyncSession,
) -> None:
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()
    result = _EnrichmentResult(output=_output_with_model("F150"), carfax_findings=None)

    _apply_to_lot(lot, result, raw_carfax_url=None)  # no canonical override
    await session.flush()
    assert lot.model == "F150"


@pytest.mark.asyncio
async def test_process_one_applies_vpic_canonical_model(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the LLM emits 'F150', vPIC (enabled, mocked) snaps it to the
    canonical 'F-150', and that is what lands on the row."""
    session = _patched_get_session
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()
    lot_id = lot.id

    monkeypatch.setattr(
        "carbuyer.apps.enricher.enricher.settings.vpic_normalization_enabled", True,
    )
    monkeypatch.setattr(
        enricher_mod.vpic, "canonical_model", AsyncMock(return_value="F-150"),
    )
    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"
    provider.describe = AsyncMock(return_value=_output_with_model("F150"))

    outcome = await _process_one(lot_id, provider=provider)

    assert outcome == "done"
    session.expire_all()
    refreshed = await session.get(AuctionLot, lot_id)
    assert refreshed is not None
    assert refreshed.model == "F-150"


# ─── NHTSA reliability signal ───


@pytest.mark.asyncio
async def test_reliability_noop_when_nhtsa_disabled() -> None:
    # Off by default → no counts, no HTTP client opened.
    assert await _reliability("Ford", "F-150", 2015) == (None, None)


@pytest.mark.asyncio
async def test_apply_to_lot_writes_reliability_counts(session: AsyncSession) -> None:
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()
    result = _EnrichmentResult(output=_output_with_model("F-150"), carfax_findings=None)

    _apply_to_lot(lot, result, raw_carfax_url=None, recall_count=3, complaint_count=47)
    await session.flush()
    expected_recalls, expected_complaints = 3, 47
    assert lot.recall_count == expected_recalls
    assert lot.complaint_count == expected_complaints


@pytest.mark.asyncio
async def test_process_one_applies_nhtsa_reliability(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    _, lot = _seed_auction_and_lot(session)
    session.add(lot)
    await session.flush()
    lot_id = lot.id

    monkeypatch.setattr(
        "carbuyer.apps.enricher.enricher.settings.nhtsa_reliability_enabled", True,
    )
    monkeypatch.setattr(
        enricher_mod.nhtsa, "fetch_reliability", AsyncMock(return_value=(3, 47)),
    )
    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"
    provider.describe = AsyncMock(return_value=_output_with_model("F-150"))

    outcome = await _process_one(lot_id, provider=provider)

    assert outcome == "done"
    session.expire_all()
    refreshed = await session.get(AuctionLot, lot_id)
    assert refreshed is not None
    expected_recalls, expected_complaints = 3, 47
    assert refreshed.recall_count == expected_recalls
    assert refreshed.complaint_count == expected_complaints


# ─── WG3: want-gated enrichment ───


def _want(name: str, **crit: object) -> Search:
    return Search(name=name, config=WantCriteria(**crit).model_dump(mode="json"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_process_one_skips_lot_matching_no_active_want(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    _, lot = _seed_auction_and_lot(session)  # title "2010 Ford F150"
    session.add(lot)
    await session.flush()
    lot_id = lot.id
    session.add(_want("nissan only", makes=["Nissan"]))  # does NOT match the Ford
    await session.commit()

    provider = MagicMock()
    provider.describe = AsyncMock(return_value=_output_with_model("F-150"))

    outcome = await _process_one(lot_id, provider=provider)

    assert outcome == "skipped"
    provider.describe.assert_not_awaited()  # the LLM was never called
    session.expire_all()
    refreshed = await session.get(AuctionLot, lot_id)
    assert refreshed is not None
    assert refreshed.enrichment_status == EnrichmentStatus.SKIPPED
    assert refreshed.notification_status == NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_process_one_enriches_lot_matching_a_want(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    _, lot = _seed_auction_and_lot(session)  # title "2010 Ford F150"
    session.add(lot)
    await session.flush()
    lot_id = lot.id
    session.add(_want("ford", makes=["Ford"]))  # matches via the title
    await session.commit()

    provider = MagicMock()
    provider.client = MagicMock()
    provider.model = "gpt-4o-mini"
    provider.describe = AsyncMock(return_value=_output_with_model("F-150"))

    outcome = await _process_one(lot_id, provider=provider)

    assert outcome == "done"
    provider.describe.assert_awaited()
