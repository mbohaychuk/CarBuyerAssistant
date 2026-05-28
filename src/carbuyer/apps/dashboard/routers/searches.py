from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import (
    CurrentUser,
    current_user,
    get_session,
)
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch
from carbuyer.db.notify import notify

router = APIRouter()

_SEARCH_CHANNEL = "saved_search_changed"


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


def _clean_int(value: str | None) -> int | None:
    v = _clean_str(value)
    return int(v) if v is not None else None


def _clean_list(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    cleaned = [v.strip() for v in values if v.strip()]
    return cleaned or None


def _apply_form(
    search: SavedSearch, *,
    name: str, make: str | None, model: str | None, trim: str | None,
    year_min: str | None, year_max: str | None, mileage_km_max: str | None,
    max_all_in_cost_cad: str | None,
    title_status: list[str] | None, condition_categorical: list[str] | None,
    province: list[str] | None, is_active: bool,
) -> None:
    search.name = name.strip() or "Untitled search"
    search.make = _clean_str(make)
    search.model = _clean_str(model)
    search.trim = _clean_str(trim)
    search.year_min = _clean_int(year_min)
    search.year_max = _clean_int(year_max)
    search.mileage_km_max = _clean_int(mileage_km_max)
    search.max_all_in_cost_cad = _clean_int(max_all_in_cost_cad)
    search.title_status = _clean_list(title_status)
    search.condition_categorical = _clean_list(condition_categorical)
    search.province = _clean_list(province)
    search.is_active = is_active


_MATCH_PAGE_SIZE = 20


async def _match_count(session: AsyncSession, search_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(SavedSearchMatch)
        .join(
            AuctionLot,
            (AuctionLot.id == SavedSearchMatch.source_id)
            & (SavedSearchMatch.source_kind == "auction_lot"),
        )
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            SavedSearchMatch.dismissed_at.is_(None),
            (AuctionLot.user_action.is_(None))
            | (AuctionLot.user_action != UserAction.PASSED.value),
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def _new_count(session: AsyncSession, search: SavedSearch) -> int:
    """Live matches newer than the last detail-view visit (spec's 'N new'),
    excluding passed lots so the badge agrees with the detail page."""
    stmt = (
        select(func.count())
        .select_from(SavedSearchMatch)
        .join(
            AuctionLot,
            (AuctionLot.id == SavedSearchMatch.source_id)
            & (SavedSearchMatch.source_kind == "auction_lot"),
        )
        .where(
            SavedSearchMatch.saved_search_id == search.id,
            SavedSearchMatch.dismissed_at.is_(None),
            (AuctionLot.user_action.is_(None))
            | (AuctionLot.user_action != UserAction.PASSED.value),
        )
    )
    if search.last_viewed_at is not None:
        stmt = stmt.where(SavedSearchMatch.matched_at > search.last_viewed_at)
    return (await session.execute(stmt)).scalar_one()


@router.get("/searches", response_class=HTMLResponse)
async def list_searches(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    searches = list((await session.execute(
        select(SavedSearch).order_by(SavedSearch.created_at.desc())
    )).scalars().all())
    counts = {s.id: await _match_count(session, s.id) for s in searches}
    new_counts = {s.id: await _new_count(session, s) for s in searches}
    return templates.TemplateResponse(
        request, "pages/searches_list.html",
        {
            "searches": searches, "counts": counts,
            "new_counts": new_counts, "active_subtab": "searches",
        },
    )


@router.get("/searches/new", response_class=HTMLResponse)
async def new_search(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/search_form.html", {"search": None},
    )


@router.post("/searches")
async def create_search(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    name: Annotated[str, Form()] = "",
    make: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    trim: Annotated[str | None, Form()] = None,
    year_min: Annotated[str | None, Form()] = None,
    year_max: Annotated[str | None, Form()] = None,
    mileage_km_max: Annotated[str | None, Form()] = None,
    max_all_in_cost_cad: Annotated[str | None, Form()] = None,
    title_status: Annotated[list[str] | None, Form()] = None,
    condition_categorical: Annotated[list[str] | None, Form()] = None,
    province: Annotated[list[str] | None, Form()] = None,
) -> Response:
    search = SavedSearch(name="x")
    _apply_form(
        search, name=name, make=make, model=model, trim=trim,
        year_min=year_min, year_max=year_max, mileage_km_max=mileage_km_max,
        max_all_in_cost_cad=max_all_in_cost_cad, title_status=title_status,
        condition_categorical=condition_categorical, province=province,
        is_active=True,
    )
    session.add(search)
    await session.flush()
    await notify(session, _SEARCH_CHANNEL, str(search.id))
    await session.commit()
    return RedirectResponse(f"/searches/{search.id}", status_code=303)


@router.get("/searches/{search_id}", response_class=HTMLResponse)
async def search_detail(
    request: Request,
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    page: int = 1,
) -> HTMLResponse:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return HTMLResponse("Not found", status_code=404)
    page = max(page, 1)

    # Current matches (paginated): join to the lot + auction, drop dismissed and
    # passed. Fetch one extra row to detect a next page without a COUNT.
    base = (
        select(AuctionLot, Auction, SavedSearchMatch)
        .join(SavedSearchMatch, (SavedSearchMatch.source_kind == "auction_lot")
              & (SavedSearchMatch.source_id == AuctionLot.id))
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            SavedSearchMatch.dismissed_at.is_(None),
            (AuctionLot.user_action.is_(None))
            | (AuctionLot.user_action != UserAction.PASSED.value),
        )
        .order_by(SavedSearchMatch.matched_at.desc())
    )
    rows = (await session.execute(
        base.offset((page - 1) * _MATCH_PAGE_SIZE).limit(_MATCH_PAGE_SIZE + 1)
    )).all()
    has_next = len(rows) > _MATCH_PAGE_SIZE
    matches = [
        {"lot": lot, "auction": auc, "match": m}
        for lot, auc, m in rows[:_MATCH_PAGE_SIZE]
    ]

    # Match-over-time activity log: every match incl. dismissed, excl. passed, newest first.
    log_rows = (await session.execute(
        select(SavedSearchMatch, AuctionLot.title)
        .join(AuctionLot, (SavedSearchMatch.source_kind == "auction_lot")
              & (SavedSearchMatch.source_id == AuctionLot.id))
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            (AuctionLot.user_action.is_(None))
            | (AuctionLot.user_action != UserAction.PASSED.value),
        )
        .order_by(SavedSearchMatch.matched_at.desc())
        .limit(50)
    )).all()
    activity = [{"match": m, "title": title} for m, title in log_rows]

    # Mark visited so the list's "N new" badge resets.
    search.last_viewed_at = datetime.now(UTC)
    await session.commit()

    return templates.TemplateResponse(
        request, "pages/search_detail.html",
        {
            "search": search, "matches": matches, "activity": activity,
            "page": page, "has_next": has_next, "active_subtab": "searches",
        },
    )


@router.get("/searches/{search_id}/edit", response_class=HTMLResponse)
async def edit_search(
    request: Request,
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request, "partials/search_form.html", {"search": search},
    )


@router.post("/searches/{search_id}/update")
async def update_search(
    request: Request,
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    name: Annotated[str, Form()] = "",
    make: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    trim: Annotated[str | None, Form()] = None,
    year_min: Annotated[str | None, Form()] = None,
    year_max: Annotated[str | None, Form()] = None,
    mileage_km_max: Annotated[str | None, Form()] = None,
    max_all_in_cost_cad: Annotated[str | None, Form()] = None,
    title_status: Annotated[list[str] | None, Form()] = None,
    condition_categorical: Annotated[list[str] | None, Form()] = None,
    province: Annotated[list[str] | None, Form()] = None,
    is_active: Annotated[str | None, Form()] = None,
) -> Response:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return HTMLResponse("Not found", status_code=404)
    _apply_form(
        search, name=name, make=make, model=model, trim=trim,
        year_min=year_min, year_max=year_max, mileage_km_max=mileage_km_max,
        max_all_in_cost_cad=max_all_in_cost_cad, title_status=title_status,
        condition_categorical=condition_categorical, province=province,
        is_active=(is_active is not None),
    )
    await notify(session, _SEARCH_CHANNEL, str(search.id))
    await session.commit()
    return RedirectResponse(f"/searches/{search.id}", status_code=303)


@router.post("/searches/{search_id}/dismiss/{match_id}")
async def dismiss_match(
    search_id: int,
    match_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    m = await session.get(SavedSearchMatch, match_id)
    if m is None or m.saved_search_id != search_id:
        return Response(status_code=404)
    if m.dismissed_at is None:
        m.dismissed_at = datetime.now(UTC)
    await session.commit()
    return Response("", status_code=200, media_type="text/html")


@router.post("/searches/{search_id}/delete")
async def delete_search(
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return Response(status_code=404)
    await session.delete(search)  # FK ondelete=CASCADE removes match rows
    await session.commit()
    return RedirectResponse("/searches", status_code=303)
