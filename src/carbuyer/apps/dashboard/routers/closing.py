from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import OPEN_STATUSES, get_session
from carbuyer.db.models import Auction, AuctionLot

router = APIRouter()

_LIMIT = 50


@router.get("/closing", response_class=HTMLResponse)
async def closing(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    hours: int = 24,
) -> HTMLResponse:
    cutoff = datetime.now(UTC) + timedelta(hours=hours)
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.lot_status.in_(OPEN_STATUSES),
            Auction.scheduled_end_at.is_not(None),
            Auction.scheduled_end_at <= cutoff,
        )
        .order_by(Auction.scheduled_end_at.asc())
        .limit(_LIMIT)
    )
    rows = (await session.execute(stmt)).all()
    items: list[dict[str, Any]] = [
        {"lot": lot, "auction": auc} for (lot, auc) in rows
    ]
    return templates.TemplateResponse(
        request,
        "pages/closing.html",
        {"items": items, "hours": hours},
    )
