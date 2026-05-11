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

_YEAR_WINDOW = 2
_LIMIT = 200


@router.get("/comps", response_class=HTMLResponse)
async def comps(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    make: str | None = None,
    model: str | None = None,
    year: int | None = None,
    trim: str | None = None,
) -> HTMLResponse:
    rows: list[HistoricalSale] = []
    if make and model:
        stmt = select(HistoricalSale).where(
            HistoricalSale.make == make,
            HistoricalSale.model == model,
        )
        if year is not None:
            stmt = stmt.where(
                HistoricalSale.year.between(year - _YEAR_WINDOW, year + _YEAR_WINDOW),
            )
        if trim:
            stmt = stmt.where(HistoricalSale.trim == trim)
        rows = list((await session.execute(stmt.limit(_LIMIT))).scalars().all())
    return templates.TemplateResponse(
        request,
        "pages/comps.html",
        {
            "rows": rows,
            "make": make,
            "model": model,
            "year": year,
            "trim": trim,
        },
    )
