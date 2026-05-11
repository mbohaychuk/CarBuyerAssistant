"""Vision-batcher — nightly cron worker.

Cron-driven nightly batch — process the deal-score-shortlisted PENDING lots
and exit.  Re-runs the next night will pick up new PENDING lots.  This worker
is NOT LISTEN-driven; it is intended to be invoked by an external cron job
(e.g. systemd timer or Kubernetes CronJob).

Score-gated shortlist: the top-N lots by price_deal_score (above a minimum
threshold) with vision_status=PENDING and an open lot_status are selected.
This keeps the nightly cost proportional to deal quality — lots that don't look
like good buys are skipped until a bid update elevates their score.

Transaction discipline (same principle as enricher / bid-poller): HTTP I/O for
photo downloads and LLM I/O for vision inference happen OUTSIDE any DB
transaction.  The pattern per lot is:
  1. Short read tx  → load snapshot, close connection.
  2. HTTP + LLM I/O outside any open transaction.
  3. Short write tx → re-fetch lot by id, apply result, NOTIFY if rescore needed.

Pessimistic-condition-override: when vision_confidence > threshold and the
vision-derived condition is ≥2 buckets below the description condition, we
revise condition_categorical downward, append a synthetic red flag, set
valuation_status=PENDING, and NOTIFY valuation_pending — the valuator then
rescores with the corrected condition.

Per-lot tempdir cleanup: each lot gets a fresh tempfile.TemporaryDirectory
whose lifecycle is managed by a context-manager so downloaded photos are
cleaned up immediately after use, preventing unbounded /tmp growth over a
nightly batch of hundreds of lots.

Consider promoting the module-level tuning constants to Settings if ops needs
to adjust them at runtime without a code deploy.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.vision_batcher.photos import download_and_resize
from carbuyer.db.enums import LotStatus, ValuationStatus, VisionStatus
from carbuyer.db.models import AuctionLot
from carbuyer.db.notify import notify
from carbuyer.db.session import get_session
from carbuyer.llm.base import VisionInput
from carbuyer.llm.openai_provider import OpenAIProvider
from carbuyer.llm.schemas import VisionOutput
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("vision_batcher")

# Minimum price_deal_score to enter the nightly shortlist. Lots below this are
# unlikely to be worth the vision API cost tonight.
_SHORTLIST_SCORE_THRESHOLD = 0.10

# Maximum lots to process per nightly run. At ~9 LLM calls per lot
# (8 per-image + 1 aggregation), 100 lots ≈ 900 calls.
_SHORTLIST_LIMIT = 100

# Minimum vision_confidence required to apply the pessimistic condition override.
# Below this, the vision pass is too uncertain to override the description.
_PESSIMISM_CONFIDENCE_THRESHOLD = 0.7

# Minimum absolute bucket distance (bad=0 … great=4) between vision-derived
# condition and description condition before we apply the pessimistic override.
# 2 means at least two full steps apart (e.g. "poor" vs "good").
_PESSIMISM_BUCKET_DIFF_MIN = 2

CONDITION_RANK: dict[str, int] = {
    "bad": 0,
    "poor": 1,
    "decent": 2,
    "good": 3,
    "great": 4,
}


@dataclass(slots=True, frozen=True)
class _LotSnapshot:
    """Frozen snapshot of the lot fields needed for vision, read in a short tx.

    Extracted before closing the session so no DB connection is held during
    photo downloads or LLM calls.
    """

    lot_id: int
    photos: list[str]
    year: int | None
    make: str | None
    model: str | None
    condition_categorical: str | None
    red_flags: list[dict[str, object]]
    green_flags: list[dict[str, object]]


def _bucket_diff(a: str | None, b: str | None) -> int:
    """Absolute condition-rank distance between two bucket strings.

    Returns 0 if either argument is None (cannot compare an unknown condition)
    or if either string is not in CONDITION_RANK (treats unknown strings as
    rank 2, so diff is 0 for both unknowns).
    """
    if a is None or b is None:
        return 0
    return abs(CONDITION_RANK.get(a, 2) - CONDITION_RANK.get(b, 2))


def _apply_vision(lot: AuctionLot, out: VisionOutput) -> bool:
    """Mutate lot with vision output. Pure assignment — no I/O.

    Returns True iff the pessimistic-condition override fired (caller uses this
    to decide whether to NOTIFY ``valuation_pending`` — re-checking
    ``lot.valuation_status`` would over-notify on lots that were already
    PENDING when vision started, e.g. fresh rescrapes).

    The override revises condition_categorical down to the vision value,
    appends a synthetic red flag, and marks valuation_status PENDING so the
    valuator rescores.
    """
    lot.vision_findings = out.model_dump()
    lot.vision_condition_overall = out.overall_vision_condition
    lot.vision_confidence = out.vision_confidence
    lot.vision_contradictions = out.contradictions_with_description

    override_fired = False
    if (
        out.vision_confidence > _PESSIMISM_CONFIDENCE_THRESHOLD
        and _bucket_diff(
            out.overall_vision_condition,
            lot.condition_categorical,
        )
        >= _PESSIMISM_BUCKET_DIFF_MIN
    ):
        # Vision sees significantly worse condition than description claimed AND
        # confidence is high — revise downward rather than trusting the text.
        vision_rank = CONDITION_RANK[out.overall_vision_condition]
        desc_rank = CONDITION_RANK[lot.condition_categorical or "decent"]
        if vision_rank < desc_rank:
            lot.condition_categorical = out.overall_vision_condition
        flags = list(lot.red_flags or [])
        # Synthetic vision-only flag — not part of the description taxonomy.
        # Fall back to a generic message when the LLM produced no contradiction
        # strings, so dashboard surfaces something readable.
        evidence = (
            ", ".join(out.contradictions_with_description)
            or "vision condition mismatch"
        )
        flags.append(
            {
                "flag": "description_oversells_condition",
                "evidence": evidence,
                "weight": -2,
            }
        )
        lot.red_flags = flags
        lot.valuation_status = ValuationStatus.PENDING
        override_fired = True

    lot.vision_status = VisionStatus.DONE
    return override_fired


async def _write_status(lot_id: int, status: VisionStatus) -> None:
    """Write a terminal status (SKIPPED / FAILED) in a short write tx."""
    async with get_session() as s, s.begin():
        lot = await s.get(AuctionLot, lot_id)
        if lot is None:
            return
        lot.vision_status = status


async def _select_shortlist(
    session: AsyncSession,
    *,
    threshold: float = _SHORTLIST_SCORE_THRESHOLD,
    limit: int = _SHORTLIST_LIMIT,
) -> list[int]:
    """Return lot ids ordered by price_deal_score desc — the nightly shortlist.

    Filters to: vision_status PENDING, lot_status open/closing_soon/extended,
    price_deal_score >= threshold.  Returns ids only so the session can be
    closed before any I/O.
    """
    stmt = (
        select(AuctionLot.id)
        .where(
            AuctionLot.vision_status == VisionStatus.PENDING,
            AuctionLot.price_deal_score >= threshold,
            AuctionLot.lot_status.in_(
                [LotStatus.OPEN, LotStatus.CLOSING_SOON, LotStatus.EXTENDED],
            ),
        )
        .order_by(AuctionLot.price_deal_score.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _process_one(
    lot_id: int,
    *,
    provider: OpenAIProvider,
) -> str:
    """Full vision pipeline for one lot. Returns 'done' | 'skipped' | 'failed' | 'missing'.

    Transaction discipline:
      1. Short read tx  — snapshot fields, close connection.
      2. tempdir + HTTP + LLM I/O — no open transaction.
      3. Short write tx — re-fetch by id, apply result, NOTIFY if rescore needed.
    """
    # 1. Snapshot in a short read tx.
    async with get_session() as s:
        lot = await s.get(AuctionLot, lot_id)
        if lot is None:
            log.warning("lot disappeared before vision", lot_id=lot_id)
            return "missing"
        snapshot = _LotSnapshot(
            lot_id=lot.id,
            photos=list(lot.photos or []),
            year=lot.year,
            make=lot.make,
            model=lot.model,
            condition_categorical=lot.condition_categorical,
            red_flags=[dict(f) for f in (lot.red_flags or [])],
            green_flags=[dict(f) for f in (lot.green_flags or [])],
        )

    # 2. HTTP + LLM I/O outside any transaction.
    if not snapshot.photos:
        await _write_status(lot_id, VisionStatus.SKIPPED)
        return "skipped"

    with tempfile.TemporaryDirectory() as tmp:
        paths = await download_and_resize(snapshot.photos, tmp_dir=Path(tmp))
        if not paths:
            await _write_status(lot_id, VisionStatus.SKIPPED)
            return "skipped"

        payload = VisionInput(
            lot_id=snapshot.lot_id,
            photo_paths=[str(p) for p in paths],
            year=snapshot.year,
            make=snapshot.make,
            model=snapshot.model,
            description_condition=snapshot.condition_categorical,
            description_red_flags=[str(f.get("flag", "")) for f in snapshot.red_flags],
            description_green_flags=[str(f.get("flag", "")) for f in snapshot.green_flags],
        )
        try:
            out = await provider.vision(payload)
        except Exception:
            log.exception("vision LLM call failed", lot_id=lot_id)
            await _write_status(lot_id, VisionStatus.FAILED)
            return "failed"

    # 3. Apply result in a short write tx; NOTIFY only when the override fired.
    async with get_session() as s, s.begin():
        lot = await s.get(AuctionLot, lot_id)
        if lot is None:
            return "missing"
        override_fired = _apply_vision(lot, out)
        if override_fired:
            await notify(s, "valuation_pending", str(lot.id))
    return "done"


async def main() -> None:
    """Nightly cron entry point. Selects shortlist, processes each lot, exits."""
    if not settings.openai_api_key:
        log.error("OPENAI_API_KEY not configured")
        sys.exit("OPENAI_API_KEY not configured")

    async with OpenAIProvider() as provider:
        async with get_session() as s:
            shortlist = await _select_shortlist(s)
        log.info("vision shortlist", count=len(shortlist))

        counts: dict[str, int] = {"done": 0, "skipped": 0, "failed": 0, "missing": 0}
        for lot_id in shortlist:
            try:
                outcome = await _process_one(lot_id, provider=provider)
            except Exception:
                log.exception("_process_one unhandled", lot_id=lot_id)
                outcome = "failed"
            counts[outcome] = counts.get(outcome, 0) + 1

        log.info("vision batch complete", **counts)
