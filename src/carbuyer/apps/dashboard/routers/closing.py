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
    now = datetime.now(UTC)
    cutoff = now + timedelta(hours=hours)
    # 15-minute grace below now: HiBid soft-close extends actual end-time past
    # scheduled_end_at when bids land in the final minutes. A lot still legitly
    # "OPEN" with scheduled_end_at 10 min in the past is normal. But scheduled
    # ends from hours/days ago that haven't flipped to lot_status=closed are
    # stale and shouldn't surface as "closing in next 24h" — that was the bug.
    floor = now - timedelta(minutes=15)
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.lot_status.in_(OPEN_STATUSES),
            Auction.scheduled_end_at.is_not(None),
            Auction.scheduled_end_at >= floor,
            Auction.scheduled_end_at <= cutoff,
            # Non-vehicle accessories (covers, hitches, tires) come through
            # HiBid's category 700006 too. After enrichment they have no
            # year/make/model. Filter at the dashboard so user-facing lists
            # stay vehicle-only without changing ingest semantics.
            AuctionLot.year.is_not(None),
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
