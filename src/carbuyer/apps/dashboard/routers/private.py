from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import CurrentUser, current_user, get_session, is_htmx
from carbuyer.db.enums import UserAction
from carbuyer.db.models import PrivateListing

router = APIRouter()

# Cap the feed; best deals are first, so the tail is low-value anyway.
_FEED_LIMIT = 100


@router.get("/private", response_class=HTMLResponse)
async def private_feed(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Best-deals-first feed of current (non-removed, non-passed) private listings."""
    stmt = (
        select(PrivateListing)
        .where(
            PrivateListing.removed_at.is_(None),
            (PrivateListing.user_action.is_(None))
            | (PrivateListing.user_action != UserAction.PASSED.value),
        )
        .order_by(
            PrivateListing.price_deal_score.desc().nulls_last(),
            PrivateListing.first_seen_at.desc(),
        )
        .limit(_FEED_LIMIT)
    )
    listings = list((await session.execute(stmt)).scalars().all())
    return templates.TemplateResponse(
        request, "pages/private.html", {"listings": listings},
    )


_PRIVATE_ACTIONS = {"interested", "passed"}


@router.post("/private/{listing_id}/mark", response_model=None)
async def mark_private(
    request: Request,
    listing_id: int,
    action: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    currently_active: Annotated[bool, Form()] = False,
) -> HTMLResponse | Response:
    """Set / toggle-off a listing's user_action (interested | passed).

    Returns the re-rendered card on HTMX requests, or an empty body when the
    new state is `passed` so the card drops out of the best-deals feed.
    """
    if action not in _PRIVATE_ACTIONS:
        raise HTTPException(status_code=422, detail=f"invalid action {action!r}")
    listing = await session.get(PrivateListing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404)

    listing.user_action = None if currently_active else UserAction(action)
    await session.commit()
    await session.refresh(listing)

    if not is_htmx(request):
        return Response(status_code=204)
    if listing.user_action == UserAction.PASSED:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "partials/private_card.html", {"listing": listing},
    )
