from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import HistoricalSale

router = APIRouter()

_LIMIT = 100


@router.get("/sold", response_class=HTMLResponse)
async def sold(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    rows = list((await session.execute(
        select(HistoricalSale).order_by(HistoricalSale.id.desc()).limit(_LIMIT),
    )).scalars().all())
    return templates.TemplateResponse(
        request, "pages/sold.html", {"rows": rows},
    )
