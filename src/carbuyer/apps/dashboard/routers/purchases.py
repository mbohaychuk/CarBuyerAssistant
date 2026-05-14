from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import CurrentUser, current_user, get_session
from carbuyer.db.models import Purchase

router = APIRouter()


@router.get("/purchases", response_class=HTMLResponse)
async def purchases_list(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    rows = list((await session.execute(
        select(Purchase).order_by(Purchase.purchase_date.desc()),
    )).scalars().all())
    year = datetime.now(UTC).year
    ytd_count = (await session.execute(
        select(func.count()).select_from(Purchase)
        .where(extract("year", Purchase.purchase_date) == year),
    )).scalar_one()
    return templates.TemplateResponse(
        request,
        "pages/purchases.html",
        {"rows": rows, "ytd_count": ytd_count, "year": year},
    )


@router.post("/purchases")
async def purchases_create(
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    purchase_date: Annotated[date, Form()],
    make: Annotated[str, Form()],
    model: Annotated[str, Form()],
    year: Annotated[int, Form()],
    purchase_price_cad: Annotated[Decimal, Form()],
    province_of_purchase: Annotated[str, Form()] = "AB",
) -> RedirectResponse:
    p = Purchase(
        purchase_date=purchase_date,
        make=make,
        model=model,
        year=year,
        purchase_price_cad=purchase_price_cad,
        province_of_purchase=province_of_purchase,
    )
    session.add(p)
    await session.commit()
    return RedirectResponse("/purchases", status_code=303)
