from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot

router = APIRouter()

_VALID_TIERS: frozenset[str] = frozenset(
    {UserAction.INTERESTED.value, UserAction.MAYBE.value},
)
_LIMIT = 100


@router.get("/watched", response_class=HTMLResponse)
async def watched(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    tier: Annotated[str, Query()] = UserAction.INTERESTED.value,
) -> HTMLResponse:
    if tier not in _VALID_TIERS:
        tier = UserAction.INTERESTED.value
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.user_action == tier)
        .order_by(Auction.scheduled_end_at.asc().nulls_last())
        .limit(_LIMIT)
    )
    rows = (await session.execute(stmt)).all()
    items: list[dict[str, Any]] = [
        {"lot": lot, "auction": auc} for (lot, auc) in rows
    ]
    return templates.TemplateResponse(
        request,
        "pages/watched.html",
        {"items": items, "tier": tier},
    )
