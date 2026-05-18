from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import OPEN_STATUSES, get_session
from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
)
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale

router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
async def health(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    # /health is intentionally unauthenticated — it's the readiness probe for
    # monitoring tools. Mutating endpoints exercise the current_user seam.
    auction_count = (await session.execute(
        select(func.count()).select_from(Auction),
    )).scalar_one()
    lot_count = (await session.execute(
        select(func.count()).select_from(AuctionLot),
    )).scalar_one()
    open_count = (await session.execute(
        select(func.count()).select_from(AuctionLot)
        .where(AuctionLot.lot_status.in_(OPEN_STATUSES)),
    )).scalar_one()
    pending_enrichment = (await session.execute(
        select(func.count()).select_from(AuctionLot)
        .where(AuctionLot.enrichment_status == EnrichmentStatus.PENDING.value),
    )).scalar_one()
    pending_valuation = (await session.execute(
        select(func.count()).select_from(AuctionLot)
        .where(AuctionLot.valuation_status == ValuationStatus.PENDING.value),
    )).scalar_one()
    pending_notification = (await session.execute(
        select(func.count()).select_from(AuctionLot)
        .where(AuctionLot.notification_status == NotificationStatus.PENDING.value),
    )).scalar_one()
    historical_count = (await session.execute(
        select(func.count()).select_from(HistoricalSale),
    )).scalar_one()
    return templates.TemplateResponse(
        request,
        "pages/health.html",
        {
            "auction_count": auction_count,
            "lot_count": lot_count,
            "open_count": open_count,
            "pending_enrichment": pending_enrichment,
            "pending_valuation": pending_valuation,
            "pending_notification": pending_notification,
            "historical_count": historical_count,
        },
    )
