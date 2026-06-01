from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import (
    CurrentUser,
    current_user,
    get_session,
    is_htmx,
)
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, SavedSearch, SavedSearchMatch
from carbuyer.db.notify import notify

router = APIRouter()

_SEARCH_CHANNEL = "saved_search_changed"


def _redirect_after_mutation(request: Request, url: str) -> Response:
    """HTMX requests get an HX-Redirect header (clean client-side navigation);
    non-HTMX form posts get a normal 303 so the no-JS fallback still works."""
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


def _clean_int(value: str | None, field: str) -> int | None:
    v = _clean_str(value)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} must be a whole number") from None


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
    search.year_min = _clean_int(year_min, "Year min")
    search.year_max = _clean_int(year_max, "Year max")
    search.mileage_km_max = _clean_int(mileage_km_max, "Max mileage")
    search.max_all_in_cost_cad = _clean_int(max_all_in_cost_cad, "Max all-in cost")
    search.title_status = _clean_list(title_status)
    search.condition_categorical = _clean_list(condition_categorical)
    search.province = _clean_list(province)
    search.is_active = is_active


_MATCH_PAGE_SIZE = 20

_MATCH_KINDS: tuple[tuple[type[AuctionLot] | type[PrivateListing], str], ...] = (
    (AuctionLot, "auction_lot"),
    (PrivateListing, "private_listing"),
)


def _live_match_count_stmt(
    model: type[AuctionLot] | type[PrivateListing],
    source_kind: str,
    search_id: int,
    *,
    since: datetime | None = None,
):
    """COUNT of live (non-dismissed, non-passed) matches of one kind for a search.

    ``since`` (a search's ``last_viewed_at``) restricts to matches after that time.
    Both AuctionLot and PrivateListing expose the same `id` / `user_action`
    columns, so one helper covers both kinds.
    """
    stmt = (
        select(func.count())
        .select_from(SavedSearchMatch)
        .join(
            model,
            (model.id == SavedSearchMatch.source_id)
            & (SavedSearchMatch.source_kind == source_kind),
        )
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            SavedSearchMatch.dismissed_at.is_(None),
            (model.user_action.is_(None))
            | (model.user_action != UserAction.PASSED.value),
        )
    )
    if since is not None:
        stmt = stmt.where(SavedSearchMatch.matched_at > since)
    return stmt


async def _match_count(session: AsyncSession, search_id: int) -> int:
    """Count live (non-dismissed, non-passed) matches across both kinds."""
    total = 0
    for model, kind in _MATCH_KINDS:
        total += (await session.execute(
            _live_match_count_stmt(model, kind, search_id)
        )).scalar_one()
    return total


async def _new_count(session: AsyncSession, search: SavedSearch) -> int:
    """Live matches of both kinds newer than the last detail-view visit
    (spec's 'N new'), excluding passed so the badge agrees with the detail page."""
    total = 0
    for model, kind in _MATCH_KINDS:
        total += (await session.execute(
            _live_match_count_stmt(model, kind, search.id, since=search.last_viewed_at)
        )).scalar_one()
    return total


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
    return _redirect_after_mutation(request, f"/searches/{search.id}")


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
        .order_by(SavedSearchMatch.matched_at.desc(), SavedSearchMatch.id.desc())
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
        .order_by(SavedSearchMatch.matched_at.desc(), SavedSearchMatch.id.desc())
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
    return _redirect_after_mutation(request, f"/searches/{search.id}")


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
    request: Request,
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return Response(status_code=404)
    await session.delete(search)  # FK ondelete=CASCADE removes match rows
    await session.commit()
    return _redirect_after_mutation(request, "/searches")
