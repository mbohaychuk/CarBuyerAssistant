from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import CurrentUser, current_user, get_session
from carbuyer.db.models import AuctionLot, WantMatch
from carbuyer.wants import repo, service
from carbuyer.wants.criteria import WantCriteria, first_error

router = APIRouter()


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


async def _render_list(
    request: Request, session: AsyncSession, *, error: str | None = None
) -> HTMLResponse:
    wants = await repo.list_wants(session)
    count_rows = (
        await session.execute(
            select(WantMatch.search_id, func.count())
            .where(WantMatch.dismissed.is_(False))
            .group_by(WantMatch.search_id)
        )
    ).all()
    counts = {search_id: n for search_id, n in count_rows}
    items: list[dict[str, Any]] = []
    for w in wants:
        try:
            criteria: WantCriteria | None = WantCriteria.model_validate(w.config)
        except ValidationError:
            criteria = None  # a corrupt/stale config must not 500 the page
        items.append({"want": w, "criteria": criteria, "match_count": counts.get(w.id, 0)})
    return templates.TemplateResponse(
        request, "pages/wants.html", {"items": items, "error": error}
    )


@router.get("/", response_class=HTMLResponse)  # want-list is the dashboard landing
@router.get("/wants", response_class=HTMLResponse)
async def wants_list(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    return await _render_list(request, session)


@router.post("/wants", response_model=None)
async def wants_create(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    name: Annotated[str, Form()],
    makes: Annotated[str | None, Form()] = None,
    models: Annotated[str | None, Form()] = None,
    trims: Annotated[str | None, Form()] = None,
    transmissions: Annotated[str | None, Form()] = None,
    drivetrains: Annotated[str | None, Form()] = None,
    year_min: Annotated[str | None, Form()] = None,
    year_max: Annotated[str | None, Form()] = None,
    max_price_cad: Annotated[str | None, Form()] = None,
    max_mileage_km: Annotated[str | None, Form()] = None,
    provinces: Annotated[str | None, Form()] = None,
    condition_min: Annotated[str | None, Form()] = None,
) -> HTMLResponse | RedirectResponse:
    try:
        criteria = WantCriteria.from_inputs(
            makes=makes, models=models, trims=trims,
            transmissions=transmissions, drivetrains=drivetrains,
            year_min=_int_or_none(year_min), year_max=_int_or_none(year_max),
            max_price_cad=_int_or_none(max_price_cad),
            max_mileage_km=_int_or_none(max_mileage_km),
            provinces=provinces, condition_min=condition_min,
        )
        want = await repo.create_want(session, name=name, criteria=criteria)
        await service.backfill_want(session, want)  # seed matches from existing lots
    except ValidationError as exc:
        return await _render_list(request, session, error=f"Invalid want: {first_error(exc)}")
    except ValueError as exc:
        return await _render_list(request, session, error=f"Invalid want: {exc}")
    await session.commit()
    return RedirectResponse("/wants", status_code=303)


@router.post("/wants/{want_id}/toggle", response_model=None)
async def wants_toggle(
    want_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> RedirectResponse:
    want = await repo.get_want(session, want_id)
    if want is not None:
        await repo.update_want(session, want_id, enabled=not want.enabled)
        await session.commit()
    return RedirectResponse("/wants", status_code=303)


@router.post("/wants/{want_id}/delete", response_model=None)
async def wants_delete(
    want_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> RedirectResponse:
    await repo.delete_want(session, want_id)
    await session.commit()
    return RedirectResponse("/wants", status_code=303)


@router.get("/wants/{want_id}", response_class=HTMLResponse)
async def want_detail(
    request: Request,
    want_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    want = await repo.get_want(session, want_id)
    if want is None:
        raise HTTPException(status_code=404)
    rows = (
        await session.execute(
            select(WantMatch, AuctionLot)
            .join(AuctionLot, AuctionLot.id == WantMatch.lot_id)
            .where(WantMatch.search_id == want_id, WantMatch.dismissed.is_(False))
            .order_by(WantMatch.want_relative_score.desc().nulls_last())
        )
    ).all()
    items: list[dict[str, Any]] = [{"match": wm, "lot": lot} for (wm, lot) in rows]
    return templates.TemplateResponse(
        request, "pages/want_detail.html", {"want": want, "items": items}
    )


@router.post("/want-matches/{match_id}/dismiss", response_model=None)
async def dismiss_match(
    match_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> RedirectResponse:
    match = await session.get(WantMatch, match_id)
    if match is None:
        return RedirectResponse("/wants", status_code=303)
    match.dismissed = True
    target = match.search_id
    await session.commit()
    return RedirectResponse(f"/wants/{target}", status_code=303)
