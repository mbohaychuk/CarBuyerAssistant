from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import OPEN_STATUSES, get_session, is_htmx
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot

router = APIRouter()

# Spec §6: lot cards ranked by 0.5 × rarity_score + 0.5 × price_deal_score.
# Each weight is independently configurable later — pinned here so it stays
# visible at the query callsite (changes to either invalidate cached cursors,
# which is why this lives next to the order_by, not in a Settings field).
_RARITY_WEIGHT = 0.5
_PRICE_WEIGHT = 0.5


@router.get("/", response_class=HTMLResponse)
async def feed(  # noqa: PLR0913
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    province: Annotated[list[str] | None, Query()] = None,
    min_score: float = 0.0,
    min_rarity: float = 0.0,
    exclude_not_interested: bool = True,
    cursor: int | None = None,
    cursor_score: float | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> HTMLResponse:
    # Blended score expression — NULLs coalesced to 0 so unscored lots tie at
    # the bottom of the ranking. The score column is also selected so the
    # next-page cursor can carry it.
    blended = (
        _PRICE_WEIGHT * func.coalesce(AuctionLot.price_deal_score, 0.0)
        + _RARITY_WEIGHT * func.coalesce(AuctionLot.rarity_score, 0.0)
    ).label("blended_score")

    stmt = (
        select(AuctionLot, Auction, blended)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.lot_status.in_(OPEN_STATUSES),
            # Non-vehicle accessories (covers, hitches, tires) come through
            # HiBid's category 700006 too; after enrichment they have no
            # year/make/model. Hide from the feed. Watched stays unfiltered
            # — a user who marked Interested wants to see the lot regardless.
            AuctionLot.year.is_not(None),
        )
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
    # Composite cursor: (blended_score, id) DESC ordering means "next page" is
    # rows strictly less than the boundary. cursor_score is sent by the HTMX
    # template along with cursor; old clients that only pass cursor get the
    # id-only fallback (which is correct for the common all-zeros-score case,
    # and acceptable drift if scores are differentiated).
    if cursor is not None and cursor_score is not None:
        stmt = stmt.where(
            or_(
                blended < cursor_score,
                and_(blended == cursor_score, AuctionLot.id < cursor),
            ),
        )
    elif cursor is not None:
        stmt = stmt.where(AuctionLot.id < cursor)
    stmt = stmt.order_by(blended.desc(), AuctionLot.id.desc()).limit(limit)

    rows = (await session.execute(stmt)).all()
    items: list[dict[str, Any]] = [
        {"lot": lot, "auction": auction} for (lot, auction, _score) in rows
    ]
    if rows:
        last_lot, _last_auction, last_score = rows[-1]
        next_cursor = last_lot.id
        next_cursor_score = float(last_score) if last_score is not None else 0.0
    else:
        next_cursor = None
        next_cursor_score = None

    template = "partials/lot_list.html" if is_htmx(request) else "pages/feed.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "items": items,
            "next_cursor": next_cursor,
            "next_cursor_score": next_cursor_score,
            "filters": {
                "province": province or [],
                "min_score": min_score,
                "min_rarity": min_rarity,
                "exclude_not_interested": exclude_not_interested,
            },
        },
    )
