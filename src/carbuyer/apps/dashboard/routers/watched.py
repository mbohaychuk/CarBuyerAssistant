from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot

router = APIRouter()

_BUCKET_STATES = (
    UserAction.INTERESTED,
    UserAction.BID_PLACED,
    UserAction.PURCHASED,
    UserAction.PASSED,
)
_PER_BUCKET_LIMIT = 100


@router.get("/watched", response_class=HTMLResponse)
async def watched(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """4-bucket flat list. Kanban polish is the follow-up PR."""
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.user_action.in_([s.value for s in _BUCKET_STATES]))
        .order_by(Auction.scheduled_end_at.asc().nulls_last())
    )
    rows = (await session.execute(stmt)).all()

    buckets: dict[str, list[dict[str, Any]]] = {s.value: [] for s in _BUCKET_STATES}
    for lot, auc in rows:
        key = lot.user_action.value if lot.user_action else None
        if key in buckets and len(buckets[key]) < _PER_BUCKET_LIMIT:
            buckets[key].append({"lot": lot, "auction": auc})

    return templates.TemplateResponse(
        request,
        "pages/watched.html",
        {"buckets": buckets},
    )
