from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.enums import LotStatus
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale

router = APIRouter()

_OPEN_STATUSES: tuple[str, ...] = (
    LotStatus.OPEN.value,
    LotStatus.CLOSING_SOON.value,
    LotStatus.EXTENDED.value,
)
_YEAR_WINDOW = 2
_MILEAGE_FACTOR_LO = 0.8
_MILEAGE_FACTOR_HI = 1.2
_OPEN_COMP_LIMIT = 10
_SOLD_COMP_LIMIT = 20


@router.get("/lots/{lot_id}", response_class=HTMLResponse)
async def lot_detail(
    request: Request,
    lot_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)
    auction = await session.get(Auction, lot.auction_id)
    return templates.TemplateResponse(
        request,
        "pages/lot_detail.html",
        {"lot": lot, "auction": auction},
    )


@router.get("/lots/{lot_id}/comps", response_class=HTMLResponse)
async def lot_comps(
    request: Request,
    lot_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    lot = await session.get(AuctionLot, lot_id)
    if lot is None or lot.make is None or lot.model is None or lot.year is None:
        return templates.TemplateResponse(
            request,
            "partials/comp_panel.html",
            {"sold": [], "open": [], "fuzzy": True},
        )

    mileage = lot.mileage_km or 0
    sold_stmt = (
        select(HistoricalSale)
        .where(
            HistoricalSale.make == lot.make,
            HistoricalSale.model == lot.model,
            HistoricalSale.year.between(
                lot.year - _YEAR_WINDOW, lot.year + _YEAR_WINDOW,
            ),
        )
        .order_by(HistoricalSale.id.desc())
        .limit(_SOLD_COMP_LIMIT)
    )
    if mileage:
        mileage_lo = int(mileage * _MILEAGE_FACTOR_LO)
        mileage_hi = int(mileage * _MILEAGE_FACTOR_HI)
        sold_stmt = sold_stmt.where(
            HistoricalSale.mileage_km.between(mileage_lo, mileage_hi),
        )
    sold = list((await session.execute(sold_stmt)).scalars().all())

    open_stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.id != lot.id,
            AuctionLot.lot_status.in_(_OPEN_STATUSES),
            AuctionLot.make == lot.make,
            AuctionLot.model == lot.model,
            AuctionLot.year.between(
                lot.year - _YEAR_WINDOW, lot.year + _YEAR_WINDOW,
            ),
        )
        .order_by(Auction.scheduled_end_at.asc())
        .limit(_OPEN_COMP_LIMIT)
    )
    open_rows = (await session.execute(open_stmt)).all()
    open_lots: list[dict[str, Any]] = [
        {"lot": lot_row, "auction": auc} for (lot_row, auc) in open_rows
    ]

    fuzzy = (len(sold) + len(open_lots)) == 0
    return templates.TemplateResponse(
        request,
        "partials/comp_panel.html",
        {"sold": sold, "open": open_lots, "fuzzy": fuzzy},
    )
