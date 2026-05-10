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

from openai import APIError, RateLimitError
from pydantic import ValidationError
from sqlalchemy import func

from carbuyer.apps._runner import run_worker
from carbuyer.db.enums import EnrichmentStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import listen, notify
from carbuyer.db.queue import claim_pending_ids, select_pending_ids
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
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("enricher")

# Phase 3 design overlay #15: when condition_confidence < this, we coerce
# condition_categorical to "decent" AND set
# condition_inferred_from_sparse_listing=True so Phase 4 can apply a
# sparse-listing pessimism penalty separately.
SPARSE_LISTING_CONFIDENCE_THRESHOLD = 0.5


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
    auction_id: int
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
        lot = await s.get(AuctionLot, lot_id)
        if lot is None:
            return None
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
            is_no_reserve=lot.reserve_met is False,
        )


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


async def _maybe_carfax(
    description: str, *, provider: OpenAIProvider,
) -> tuple[str | None, CarfaxFindings | None]:
    """Best-effort: extract URL, fetch HTML, LLM-extract findings.

    Returns (canonical_url_for_storage, findings). Either / both may be
    None — Carfax is paywalled / bot-detected and most lots will skip the
    LLM extraction. Failures are not propagated; Carfax is supplementary.
    """
    url = find_carfax_url(description)
    if url is None:
        return None, None
    log.info("carfax url found", url=redact_carfax_url(url))
    html = await fetch_carfax_text(url)
    if html is None:
        return url, None
    findings = await extract_carfax_findings(
        html, client=provider.client, model=provider.model,
    )
    return url, findings


async def _compute_enrichment(
    snap: _LotSnapshot, *, provider: OpenAIProvider,
) -> _EnrichmentResult:
    """All network I/O happens here, OUTSIDE any DB transaction."""
    payload = _build_describe_input(snap)
    output = await provider.describe(payload)
    _, carfax_findings = await _maybe_carfax(snap.description, provider=provider)
    return _EnrichmentResult(output=output, carfax_findings=carfax_findings)


def _apply_to_lot(
    lot: AuctionLot,
    result: _EnrichmentResult,
    *,
    raw_carfax_url: str | None,
) -> None:
    """Mutate the ORM row with enrichment output. Caller controls the
    transaction. No I/O here — this is pure CPU + assignment.

    Phase 3 design overlay #14/#15: code-side condition clamp + sparse-listing
    flag. Phase 3 design overlay #5: year/make/model/etc. preserve existing
    values when LLM normalization returns None ("or" fallback) and preserve
    existing non-"unknown" values for transmission/drivetrain.
    """
    out = result.output
    nv = out.normalized_vehicle
    lot.year = nv.year or lot.year
    lot.make = nv.make or lot.make
    lot.model = nv.model or lot.model
    lot.trim = nv.trim or lot.trim
    lot.engine = nv.engine or lot.engine
    if nv.transmission != "unknown":
        lot.transmission = nv.transmission
    if nv.drivetrain != "unknown":
        lot.drivetrain = nv.drivetrain
    lot.mileage_km = nv.mileage_km or lot.mileage_km
    lot.vin = nv.vin or lot.vin
    lot.title_status = out.title_status

    if out.condition_confidence < SPARSE_LISTING_CONFIDENCE_THRESHOLD:
        lot.condition_categorical = "decent"
        lot.condition_inferred_from_sparse_listing = True
    else:
        lot.condition_categorical = out.condition_categorical
        lot.condition_inferred_from_sparse_listing = False
    lot.condition_confidence = out.condition_confidence
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
    if result.carfax_findings is not None:
        lot.carfax_findings = result.carfax_findings.model_dump()

    lot.enrichment_status = EnrichmentStatus.DONE
    lot.last_enrichment_error = None
    lot.valuation_status = ValuationStatus.PENDING
    lot.enrichment_version = settings.enrichment_version


def _classify_failure(exc: BaseException) -> str:
    """Return ``transient`` (leave PENDING for retry) or ``permanent`` (mark
    FAILED at attempts=1).

    SDK already retries 5xx and connection errors. By the time RateLimitError
    or APIError bubbles up we've exhausted SDK retries — but it's still worth
    one more attempt cycle (the SDK's retries don't span worker invocations).
    Schema-validation / parse errors are permanent because retrying won't
    change the model's output.
    """
    if isinstance(exc, RateLimitError | APIError | TimeoutError | ConnectionError):
        return "transient"
    if isinstance(exc, ValidationError):
        return "permanent"
    return "transient"


async def _process_one(lot_id: int, *, provider: OpenAIProvider) -> bool:
    """Process one claimed lot id end-to-end. Returns True if enriched DONE.

    Failure path increments ``enrichment_attempts`` and either keeps status
    PENDING (transient, attempts < max) or sets FAILED (permanent, or
    attempts >= max).
    """
    snap = await _load_snapshot(lot_id)
    if snap is None:
        log.warning("lot disappeared between claim and load", lot_id=lot_id)
        return False
    raw_carfax_url = find_carfax_url(snap.description)
    try:
        result = await _compute_enrichment(snap, provider=provider)
    except Exception as exc:
        classification = _classify_failure(exc)
        log.exception(
            "enrichment failed",
            lot_id=lot_id,
            classification=classification,
        )
        async with get_session() as s, s.begin():
            lot = await s.get(AuctionLot, lot_id)
            if lot is None:
                return False
            lot.enrichment_attempts = (lot.enrichment_attempts or 0) + 1
            lot.last_enrichment_error = f"{type(exc).__name__}: {exc}"[:500]
            should_fail = (
                classification == "permanent"
                or lot.enrichment_attempts >= settings.enrichment_max_attempts
            )
            if should_fail:
                lot.enrichment_status = EnrichmentStatus.FAILED
            else:
                lot.enrichment_status = EnrichmentStatus.PENDING  # re-claim
        return False

    async with get_session() as s, s.begin():
        lot = await s.get(AuctionLot, lot_id)
        if lot is None:
            return False
        lot.enrichment_attempts = (lot.enrichment_attempts or 0) + 1
        _apply_to_lot(lot, result, raw_carfax_url=raw_carfax_url)
        await notify(s, "valuation_pending", str(lot.id))
    return True


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

    async def _bounded(lot_id: int) -> None:
        async with sem:
            try:
                await _process_one(lot_id, provider=provider)
            except Exception:
                # Belt-and-suspenders: _process_one handles its own errors
                # but a bug above would otherwise sink the whole gather.
                log.exception("process_one unhandled", lot_id=lot_id)

    await asyncio.gather(*(_bounded(i) for i in ids))
    return len(ids)


async def _catchup_sweep(provider: OpenAIProvider) -> None:
    """Drain rows that were already PENDING when the worker started.

    Phase 2 design overlay #12 + Phase 3 overlay #3: every continuous worker
    must do this before entering ``LISTEN`` to recover NOTIFYs missed during
    downtime.
    """
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
    async with OpenAIProvider() as provider:
        await _catchup_sweep(provider)
        async for _payload in listen("enrichment_pending"):
            try:
                await process_pending(provider)
            except Exception:
                log.exception("batch failed; sleeping before next NOTIFY")
                await asyncio.sleep(5)


# ``func`` is imported for SQLAlchemy DDL helpers used by callers / tests of
# this module. Keep it referenced so the import isn't pruned.
_ = func


if __name__ == "__main__":
    run_worker("enricher", main)
