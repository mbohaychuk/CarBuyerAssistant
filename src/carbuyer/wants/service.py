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

from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import AuctionLot, WantMatch
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria
from carbuyer.wants.deal import score_want_deal
from carbuyer.wants.matcher import matches


async def evaluate_lot_against_wants(
    session: AsyncSession,
    lot: AuctionLot,
    *,
    pickup_province: str | None = None,
    offer_price_cad: Decimal | int | None = None,
) -> list[WantMatch]:
    created: list[WantMatch] = []
    for want in await repo.list_wants(session, enabled_only=True):
        criteria = WantCriteria.model_validate(want.config)
        if not matches(
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
