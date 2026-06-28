"""Match-on-valuation: evaluate a freshly-valued lot against the want-list.

Called by the valuator right after it writes a lot's valuation (a DB-only step,
in the valuator's transaction). It runs the matcher over every enabled want,
scores each match, and upserts the want_matches ledger. It returns only the
NEWLY created matches so the caller knows whether to enqueue a notification —
re-scoring an already-known match must not re-alert (fire-once lives in the
ledger's notified_at).

Price and pickup province are passed in (channel-specific) for the same reason
the matcher and scorer take them as keywords.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import LotStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot, Search, WantMatch
from carbuyer.shared.logging import get_logger
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria
from carbuyer.wants.deal import score_want_deal
from carbuyer.wants.matcher import matches

log = get_logger("wants")

_OPEN_STATUSES = (
    LotStatus.OPEN.value,
    LotStatus.CLOSING_SOON.value,
    LotStatus.EXTENDED.value,
)
# ponytail: load+filter open valued lots in Python (reuses the matcher predicate,
# no SQL divergence); capped for safety. Raise/window if inventory ever outgrows it.
_BACKFILL_LIMIT = 500


def _criteria_or_none(want: Search) -> WantCriteria | None:
    """Parse a stored config, skipping (not raising on) a corrupt/stale row so one
    bad want can't stall the whole valuation/notification pipeline."""
    try:
        return WantCriteria.model_validate(want.config)
    except ValidationError:
        log.warning("skipping want with invalid config", want_id=want.id)
        return None


async def evaluate_lot_against_wants(
    session: AsyncSession,
    lot: AuctionLot,
    *,
    pickup_province: str | None = None,
    offer_price_cad: Decimal | int | None = None,
) -> list[WantMatch]:
    created: list[WantMatch] = []
    for want in await repo.list_wants(session, enabled_only=True):
        criteria = _criteria_or_none(want)
        if criteria is None or not matches(
            lot,
            criteria,
            pickup_province=pickup_province,
            offer_price_cad=offer_price_cad,
        ):
            continue
        deal = score_want_deal(lot, criteria, offer_price_cad=offer_price_cad)
        match, was_created = await repo.upsert_want_match(
            session,
            search_id=want.id,
            lot_id=lot.id,
            want_relative_score=deal.score,
        )
        if was_created:
            created.append(match)
    return created


async def backfill_want(session: AsyncSession, want: Search) -> int:
    """Seed want_matches from already-valued open lots so a freshly-created want
    shows existing matches immediately. Does NOT enqueue notifications (it touches
    no notification_status) — only the forward valuator path alerts. Returns the
    number of matches written."""
    criteria = _criteria_or_none(want)
    if criteria is None:
        return 0
    rows = (
        await session.execute(
            select(AuctionLot, Auction)
            .join(Auction, Auction.id == AuctionLot.auction_id)
            .where(
                AuctionLot.lot_status.in_(_OPEN_STATUSES),
                AuctionLot.valuation_status == ValuationStatus.DONE.value,
                AuctionLot.make.is_not(None),
            )
            .limit(_BACKFILL_LIMIT)
        )
    ).all()
    count = 0
    for lot, auction in rows:
        if not matches(
            lot, criteria,
            pickup_province=auction.pickup_province,
            offer_price_cad=lot.current_high_bid_cad,
        ):
            continue
        deal = score_want_deal(lot, criteria, offer_price_cad=lot.current_high_bid_cad)
        await repo.upsert_want_match(
            session, search_id=want.id, lot_id=lot.id, want_relative_score=deal.score
        )
        count += 1
    return count
