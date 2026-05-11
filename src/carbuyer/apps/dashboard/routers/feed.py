from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session, is_htmx
from carbuyer.db.enums import LotStatus, UserAction
from carbuyer.db.models import Auction, AuctionLot

router = APIRouter()

_OPEN_STATUSES: tuple[str, ...] = (
    LotStatus.OPEN.value,
    LotStatus.CLOSING_SOON.value,
    LotStatus.EXTENDED.value,
)


@router.get("/", response_class=HTMLResponse)
async def feed(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    province: Annotated[list[str] | None, Query()] = None,
    min_score: float = 0.0,
    min_rarity: float = 0.0,
    exclude_not_interested: bool = True,
    cursor: int | None = None,
    limit: int = 20,
) -> HTMLResponse:
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.lot_status.in_(_OPEN_STATUSES))
    )
    if province:
        stmt = stmt.where(Auction.pickup_province.in_(province))
    if min_score > 0:
        stmt = stmt.where(AuctionLot.price_deal_score >= min_score)
    if min_rarity > 0:
        stmt = stmt.where(AuctionLot.rarity_score >= min_rarity)
    if exclude_not_interested:
        stmt = stmt.where(
            AuctionLot.user_action.is_distinct_from(UserAction.NOT_INTERESTED.value)
        )
    if cursor is not None:
        stmt = stmt.where(AuctionLot.id < cursor)
    stmt = stmt.order_by(AuctionLot.id.desc()).limit(limit)

    rows = (await session.execute(stmt)).all()
    items: list[dict[str, Any]] = [
        {"lot": lot, "auction": auction} for (lot, auction) in rows
    ]
    next_cursor = items[-1]["lot"].id if items else None

    template = "partials/lot_list.html" if is_htmx(request) else "pages/feed.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "items": items,
            "next_cursor": next_cursor,
            "filters": {
                "province": province or [],
                "min_score": min_score,
                "min_rarity": min_rarity,
                "exclude_not_interested": exclude_not_interested,
            },
        },
    )
