"""Description-enrichment worker.

Phase 3 design overlay (decisions #1-#16) — this worker is the canonical
implementation of the LLM-driven enrichment pipeline:

- Real queue API: ``claim_pending_ids`` returns ``list[int]``; per-id work
  opens its own fresh ``get_session()`` with a short transaction.
- All LLM and HTTP I/O happens OUTSIDE ``session.begin()`` — claim, close,
  do LLM, reopen, write. Never holds a connection across network latency.
- Catchup sweep at startup before ``LISTEN`` to recover NOTIFYs missed during
  worker downtime (Phase 2 idiom).
- Status writes use ``EnrichmentStatus.FAILED`` etc. (StrEnum members).
- ``enrichment_attempts`` counter: transient errors (RateLimitError,
  network, 5xx-via-SDK-already-retried) leave status PENDING for re-claim
  until attempts >= ``settings.enrichment_max_attempts``; schema/validation
  errors fail-fast at attempts=1.
- ``condition_confidence < 0.5`` → coerce ``condition_categorical = "decent"``
  AND set ``condition_inferred_from_sparse_listing=True`` (code-side, not
  prompt-side, per overlay #14/#15).
- Bounded LLM concurrency via ``asyncio.Semaphore``.
- ``OPENAI_API_KEY`` empty → fail-fast at startup.
- ``OpenAIProvider`` lifecycle managed via ``async with``.
- Carfax extraction passes the worker's existing ``provider.client`` —
  doesn't construct its own AsyncOpenAI per lot.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    LengthFinishReasonError,
    RateLimitError,
)
from pydantic import ValidationError

from carbuyer.apps._runner import run_worker
from carbuyer.db.enums import EnrichmentStatus, NotificationStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, VehicleOffer
from carbuyer.db.notify import listen, notify
from carbuyer.db.queue import (
    claim_pending_ids,
    recover_orphans,
    select_pending_ids,
)
from carbuyer.db.session import get_session, get_session_maker
from carbuyer.llm.base import DescribeInput
from carbuyer.llm.carfax import (
    extract_carfax_findings,
    fetch_carfax_text,
    find_carfax_url,
    redact_carfax_url,
)
from carbuyer.llm.openai_provider import OpenAIProvider
from carbuyer.llm.schemas import CarfaxFindings, EnrichmentOutput
from carbuyer.normalize import nhtsa, vpic
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock
from carbuyer.sources.http import make_client
from carbuyer.wants.matcher import could_match_any_want
from carbuyer.wants.service import load_active_criteria

log = get_logger("enricher")

# Phase 3 design overlay #15: when condition_confidence < this, we coerce
# condition_categorical to "decent" AND set
# condition_inferred_from_sparse_listing=True so Phase 4 can apply a
# sparse-listing pessimism penalty separately.
SPARSE_LISTING_CONFIDENCE_THRESHOLD = 0.5

# Phase 13 review: LLM-supplied numerics must be sanity-checked before write.
# Pydantic accepts any int; a hallucinated -50000 or a mi→km unit-swap (e.g.
# 250000 stored as km when listing said 155k miles) silently poisons the comp
# set and flips price-deal alerts. Cheap reject + log.
_MILEAGE_KM_MIN = 0
_MILEAGE_KM_MAX = 1_500_000
_YEAR_MIN = 1900

# Below this many bytes of description, we override the LLM's
# description_quality to "thin" regardless of what the model said. Matches the
# prompt's "<100 chars" boundary from the system prompt.
THIN_DESCRIPTION_BYTES = 100


@dataclass(slots=True, frozen=True)
class _LotSnapshot:
    """In-memory snapshot of an auction-lot row used to build DescribeInput
    without holding a DB session open during the LLM call."""
    lot_id: int
    title: str
    description: str
    year: int | None
    make: str | None
    model: str | None
    image_count: int
    auction_id: int | None  # None for private listings (no auction context)
    auctioneer_name: str | None
    auction_subtype: str
    pickup_province: str | None
    current_high_bid_cad: Decimal | None
    auction_close_at: datetime | None
    is_no_reserve: bool


@dataclass(slots=True, frozen=True)
class _EnrichmentResult:
    """LLM output + Carfax findings, ready to be written. Bid increment is
    not modeled per-source yet (Phase 7 territory) so we leave it out of the
    snapshot — the prompt receives None, which is honest signal."""
    output: EnrichmentOutput
    carfax_findings: CarfaxFindings | None


async def _load_snapshot(lot_id: int) -> _LotSnapshot | None:
    async with get_session() as s:
        lot = await s.get(VehicleOffer, lot_id)
        if lot is None:
            return None
        if isinstance(lot, AuctionLot):
            auction = await s.get(Auction, lot.auction_id)
            if auction is None:
                return None
            return _LotSnapshot(
                lot_id=lot.id,
                title=lot.title or "",
                description=lot.description or "",
                year=lot.year,
                make=lot.make,
                model=lot.model,
                image_count=len(lot.photos or []),
                auction_id=auction.id,
                auctioneer_name=auction.auctioneer_name,
                auction_subtype=auction.auction_subtype,
                pickup_province=auction.pickup_province,
                current_high_bid_cad=lot.current_high_bid_cad,
                auction_close_at=auction.scheduled_end_at,
                # No-reserve detection: per Phase 3 review, `reserve_met=False`
                # means "reserve exists but not yet met"; `None` means "unknown".
                # Neither maps to "no reserve". Until a true `is_no_reserve` signal
                # lands (Phase 7 bid-poller territory), tell the LLM `False`
                # honestly — better than the inverted "is False" derivation.
                is_no_reserve=False,
            )
        if isinstance(lot, PrivateListing):
            # No auction context — the LLM still normalizes make/model and reads
            # flags from the description. ``auction_subtype='private_sale'`` and
            # the asking price stand in for the auction-context fields.
            return _LotSnapshot(
                lot_id=lot.id,
                title=lot.title or "",
                description=lot.description or "",
                year=lot.year,
                make=lot.make,
                model=lot.model,
                image_count=len(lot.photos or []),
                auction_id=None,
                auctioneer_name=None,
                auction_subtype="private_sale",
                pickup_province=lot.location_province,
                current_high_bid_cad=lot.asking_price_cad,
                auction_close_at=None,
                is_no_reserve=False,
            )
        return None


def _build_describe_input(snap: _LotSnapshot) -> DescribeInput:
    return DescribeInput(
        lot_id=snap.lot_id,
        title=snap.title,
        description=snap.description,
        year=snap.year,
        make=snap.make,
        model=snap.model,
        auctioneer_name=snap.auctioneer_name,
        auction_subtype=snap.auction_subtype,
        pickup_province=snap.pickup_province,
        raw_carfax_url=find_carfax_url(snap.description),
        current_high_bid_cad=snap.current_high_bid_cad,
        bid_increment=None,
        auction_close_at=snap.auction_close_at,
        is_no_reserve=snap.is_no_reserve,
        image_count=snap.image_count,
        current_year=datetime.now(UTC).year,
    )


async def _maybe_carfax_findings(
    url: str | None, *, provider: OpenAIProvider,
) -> CarfaxFindings | None:
    """Best-effort: fetch HTML at the supplied URL and LLM-extract findings.

    Returns None when no URL provided, the fetch fails (paywall / bot-block
    / 4xx / 5xx / short body), or the LLM extraction fails. Carfax is
    supplementary signal — failures are never propagated.
    """
    if url is None:
        return None
    log.info("carfax url found", url=redact_carfax_url(url))
    html = await fetch_carfax_text(url)
    if html is None:
        return None
    return await extract_carfax_findings(
        html, client=provider.client, model=provider.model,
    )


async def _compute_enrichment(
    snap: _LotSnapshot,
    *,
    provider: OpenAIProvider,
    carfax_url: str | None,
) -> _EnrichmentResult:
    """All network I/O happens here, OUTSIDE any DB transaction."""
    payload = _build_describe_input(snap)
    output = await provider.describe(payload)
    carfax_findings = await _maybe_carfax_findings(carfax_url, provider=provider)
    return _EnrichmentResult(output=output, carfax_findings=carfax_findings)


def _is_unknown_str(s: str | None) -> bool:
    """Free-text fields like ``engine`` have no Literal — treat the strings
    'unknown' / 'n/a' (any case) as not-real-information and preserve any
    pre-existing scraped or prior-enrichment value."""
    return s is not None and s.strip().lower() in ("unknown", "n/a", "")


async def _canonical_model(make: str | None, model: str | None, year: int | None) -> str | None:
    """Best-effort NHTSA-canonical model spelling (off unless configured)."""
    if not settings.vpic_normalization_enabled:
        return model
    async with make_client(timeout=10.0) as client:
        return await vpic.canonical_model(make, model, year, client=client)


async def _reliability(
    make: str | None, model: str | None, year: int | None,
) -> tuple[int | None, int | None]:
    """Best-effort NHTSA recall + complaint counts (off unless configured)."""
    if not settings.nhtsa_reliability_enabled:
        return None, None
    async with make_client(timeout=10.0) as client:
        return await nhtsa.fetch_reliability(make, model, year, client=client)


def _apply_to_lot(  # noqa: PLR0912, PLR0915 -- field-by-field mapper; counts scale with the column set
    lot: VehicleOffer,
    result: _EnrichmentResult,
    *,
    raw_carfax_url: str | None,
    canonical_model: str | None = None,
    recall_count: int | None = None,
    complaint_count: int | None = None,
) -> None:
    """Mutate the ORM row with enrichment output. Caller controls the
    transaction. No I/O here — this is pure CPU + assignment.

    Phase 3 design overlay #14/#15: code-side condition clamp + sparse-listing
    flag. Phase 3 design overlay #5: year/make/model/etc. preserve existing
    values when LLM normalization returns None ("or" fallback) and preserve
    existing non-"unknown" values for transmission/drivetrain. Phase 3 review
    follow-ups: gate ``title_status`` and ``engine`` so a low-confidence
    re-enrichment can't regress a previously-good value to "UNKNOWN".
    """
    out = result.output
    nv = out.normalized_vehicle
    year_cap = datetime.now(UTC).year + 1
    if nv.year is not None and not (_YEAR_MIN <= nv.year <= year_cap):
        log.warning(
            "rejecting out-of-range year from LLM",
            lot_id=lot.id, llm_year=nv.year,
        )
    else:
        lot.year = nv.year or lot.year
    lot.make = nv.make or lot.make
    # vPIC-canonicalized model (when enabled) wins over the raw LLM spelling.
    lot.model = canonical_model or nv.model or lot.model
    lot.trim = nv.trim or lot.trim
    if not _is_unknown_str(nv.engine):
        lot.engine = nv.engine or lot.engine
    if nv.transmission != "unknown":
        lot.transmission = nv.transmission
    if nv.drivetrain != "unknown":
        lot.drivetrain = nv.drivetrain
    if nv.mileage_km is not None and not (
        _MILEAGE_KM_MIN <= nv.mileage_km <= _MILEAGE_KM_MAX
    ):
        log.warning(
            "rejecting out-of-range mileage from LLM",
            lot_id=lot.id, llm_mileage_km=nv.mileage_km,
        )
    else:
        lot.mileage_km = nv.mileage_km or lot.mileage_km
    lot.vin = nv.vin or lot.vin
    if out.title_status != "UNKNOWN":
        lot.title_status = out.title_status

    if out.condition_confidence < SPARSE_LISTING_CONFIDENCE_THRESHOLD:
        lot.condition_categorical = "decent"
        lot.condition_inferred_from_sparse_listing = True
    else:
        lot.condition_categorical = out.condition_categorical
        lot.condition_inferred_from_sparse_listing = False
    lot.condition_confidence = out.condition_confidence
    # Post-validate description_quality against the actual description length.
    # The model self-reports this; a 50-char listing rated "detailed" is a
    # red flag for the model, not the listing. Override to "thin" in that case.
    if len(lot.description or "") < THIN_DESCRIPTION_BYTES:
        lot.description_quality = "thin"
    else:
        lot.description_quality = out.description_quality

    lot.red_flags = [f.model_dump() for f in out.red_flags]
    lot.green_flags = [f.model_dump() for f in out.green_flags]
    lot.showstopper_flags = [f.model_dump() for f in out.showstopper_flags]
    lot.summary = out.summary
    lot.carfax_url = out.carfax_url or raw_carfax_url
    lot.desirable_trim_or_spec = out.rarity.desirable_trim_or_spec
    lot.classic_or_collector = out.rarity.classic_or_collector
    lot.desirability_signals = list(out.rarity.desirability_signals)
    lot.desirability_evidence = list(out.rarity.desirability_evidence)
    if recall_count is not None:
        lot.recall_count = recall_count
    if complaint_count is not None:
        lot.complaint_count = complaint_count
    if result.carfax_findings is not None:
        lot.carfax_findings = result.carfax_findings.model_dump()

    lot.enrichment_status = EnrichmentStatus.DONE
    lot.last_enrichment_error = None
    lot.valuation_status = ValuationStatus.PENDING
    lot.enrichment_version = settings.enrichment_version


def _classify_failure(exc: BaseException) -> str:
    """Return ``transient`` (leave PENDING for retry) or ``permanent`` (mark
    FAILED at attempts=1).

    Permanent (re-running won't change the answer):
      - ``ValidationError`` — Pydantic schema rejected the LLM output.
      - ``LengthFinishReasonError`` — model hit ``max_tokens`` mid-JSON;
        re-running with same prompt + temperature=0 produces the same truncation.
      - 4xx other than 429: ``AuthenticationError``, ``PermissionDeniedError``,
        ``BadRequestError``, ``NotFoundError``, ``UnprocessableEntityError``.
        These all subclass ``openai.APIStatusError``; the SDK does not retry
        them. Treat as permanent so a revoked-key incident doesn't burn 3
        attempt cycles per lot.

    Transient (worth re-claiming next batch):
      - ``RateLimitError`` (post-SDK-retries — the SDK's retries don't span
        worker invocations, so one more attempt later is reasonable).
      - ``APIConnectionError``, ``APITimeoutError`` — network blips.
      - ``InternalServerError`` (5xx) — past SDK retries.
      - Default for unrecognized exceptions: transient. We'd rather retry
        an unknown failure than poison-pill the lot. Permanent failures
        leave a ``last_enrichment_error`` for postmortem.
    """
    if isinstance(exc, ValidationError | LengthFinishReasonError):
        return "permanent"
    transient_types = (
        RateLimitError, APIConnectionError, APITimeoutError, InternalServerError,
    )
    if isinstance(exc, transient_types):
        return "transient"
    # Other openai.APIStatusError subclasses (Authentication, BadRequest,
    # PermissionDenied, NotFound, UnprocessableEntity) are permanent. The
    # parent-class isinstance check covers all of them at once.
    if isinstance(exc, APIStatusError):
        return "permanent"
    return "transient"


async def _process_one(lot_id: int, *, provider: OpenAIProvider) -> str:  # noqa: PLR0911 -- terminal-status state machine; each return is a distinct outcome
    """Process one claimed lot id end-to-end.

    Returns:
      - ``"done"`` — lot enriched, status DONE, valuator NOTIFY emitted.
      - ``"failed"`` — permanent failure, status FAILED.
      - ``"transient"`` — transient failure, status PENDING (re-claimable).
        Caller must re-NOTIFY ``enrichment_pending`` so the worker drains
        on the same NOTIFY-driven loop instead of waiting for the next
        worker restart's catchup sweep.
      - ``"missing"`` — lot row vanished between claim and load.
    """
    snap = await _load_snapshot(lot_id)
    if snap is None:
        log.warning("lot disappeared between claim and load", lot_id=lot_id)
        return "missing"

    # WG3 want-gate: skip the LLM for a lot that matches no active want (e.g. its
    # want was deleted between ingest and enrich). Lenient when there are no wants
    # at all — WG2 already keeps a wantless system from ingesting anything.
    async with get_session() as s:
        criteria_list = await load_active_criteria(s)
    if criteria_list and not could_match_any_want(
        make=snap.make, model=snap.model, year=snap.year, title=snap.title,
        criteria_list=criteria_list,
    ):
        async with get_session() as s, s.begin():
            lot = await s.get(VehicleOffer, lot_id)
            if lot is None:
                return "missing"
            lot.enrichment_status = EnrichmentStatus.SKIPPED
            lot.valuation_status = ValuationStatus.SKIPPED
            lot.notification_status = NotificationStatus.SKIPPED
        log.info("skipped lot matching no active want", lot_id=lot_id)
        return "skipped"

    raw_carfax_url = find_carfax_url(snap.description)
    try:
        result = await _compute_enrichment(
            snap, provider=provider, carfax_url=raw_carfax_url,
        )
    except Exception as exc:
        classification = _classify_failure(exc)
        log.exception(
            "enrichment failed",
            lot_id=lot_id,
            classification=classification,
        )
        async with get_session() as s, s.begin():
            lot = await s.get(VehicleOffer, lot_id)
            if lot is None:
                return "missing"
            lot.enrichment_attempts = (lot.enrichment_attempts or 0) + 1
            lot.last_enrichment_error = f"{type(exc).__name__}: {exc}"[:500]
            should_fail = (
                classification == "permanent"
                or lot.enrichment_attempts >= settings.enrichment_max_attempts
            )
            if should_fail:
                lot.enrichment_status = EnrichmentStatus.FAILED
                return "failed"
            lot.enrichment_status = EnrichmentStatus.PENDING  # re-claim
        return "transient"

    nv = result.output.normalized_vehicle
    year = nv.year or snap.year
    canonical = await _canonical_model(nv.make, nv.model, year)
    recall_count, complaint_count = await _reliability(nv.make, canonical or nv.model, year)

    async with get_session() as s, s.begin():
        lot = await s.get(VehicleOffer, lot_id)
        if lot is None:
            return "missing"
        lot.enrichment_attempts = (lot.enrichment_attempts or 0) + 1
        _apply_to_lot(
            lot, result, raw_carfax_url=raw_carfax_url, canonical_model=canonical,
            recall_count=recall_count, complaint_count=complaint_count,
        )
        await notify(s, "valuation_pending", str(lot.id))
    return "done"


async def process_pending(provider: OpenAIProvider) -> int:
    """Claim a batch of pending lot IDs, process each in its own transaction.

    Returns the count of IDs claimed (not successes — failures count too).
    """
    sm = get_session_maker()
    async with sm() as claim_session, claim_session.begin():
        ids = await claim_pending_ids(
            claim_session,
            status_field="enrichment_status",
            limit=settings.enrichment_batch_size,
        )
    if not ids:
        return 0

    sem = asyncio.Semaphore(settings.openai_concurrency)
    results: list[str] = []

    async def _bounded(lot_id: int) -> None:
        async with sem:
            try:
                outcome = await _process_one(lot_id, provider=provider)
            except Exception:
                # Belt-and-suspenders: _process_one handles its own errors
                # but a bug above would otherwise sink the whole gather.
                log.exception("process_one unhandled", lot_id=lot_id)
                outcome = "transient"
            results.append(outcome)

    await asyncio.gather(*(_bounded(i) for i in ids))

    # Self-NOTIFY for transient leftovers so the listener loop drains them
    # without waiting for the next worker restart's catchup sweep. Otherwise
    # rate-limit / network blips create a 24h+ delay in low-throughput
    # periods. One bulk NOTIFY (empty payload) is enough to wake the loop.
    if any(r == "transient" for r in results):
        async with get_session() as s, s.begin():
            await notify(s, "enrichment_pending", "")
    return len(ids)


async def _catchup_sweep(provider: OpenAIProvider) -> None:
    """Drain rows that were already PENDING when the worker started.

    Phase 2 design overlay #12 + Phase 3 overlay #3: every continuous worker
    must do this before entering ``LISTEN`` to recover NOTIFYs missed during
    downtime. Phase 13: prepend orphan recovery — flip IN_PROGRESS rows from
    a prior crash back to PENDING so claim_pending_ids picks them up.
    Without this, a SIGKILL between claim and write-completion strands the
    row forever (Phase 2.5 watchdog is referenced in queue.py:64 but isn't
    actually implemented yet).
    """
    async with get_session() as s, s.begin():
        recovered = await recover_orphans(s, status_field="enrichment_status")
    if recovered > 0:
        log.warning(
            "recovered orphaned IN_PROGRESS lots at startup",
            count=recovered,
            note="prior worker crash; rows reset to pending",
        )
    async with get_session() as s:
        ids = await select_pending_ids(
            s, status_field="enrichment_status", limit=10_000,
        )
    if not ids:
        log.info("catchup sweep — no pending lots")
        return
    log.info("catchup sweep starting", pending_count=len(ids))
    while True:
        n = await process_pending(provider)
        if n == 0:
            break
        log.info("catchup batch processed", count=n)
    log.info("catchup sweep complete")


async def main() -> None:
    if not settings.openai_api_key:
        # Phase 3 design overlay #16: fail at startup, not on first lot.
        log.error("OPENAI_API_KEY not configured")
        sys.exit("OPENAI_API_KEY not configured")
    lock_conn = await acquire_singleton_lock("enricher")
    try:
        async with OpenAIProvider() as provider:
            await _catchup_sweep(provider)
            async for _payload in listen("enrichment_pending"):
                try:
                    await process_pending(provider)
                except Exception:
                    log.exception("batch failed; sleeping before next NOTIFY")
                    await asyncio.sleep(5)
    finally:
        await lock_conn.close()


if __name__ == "__main__":
    run_worker("enricher", main)
