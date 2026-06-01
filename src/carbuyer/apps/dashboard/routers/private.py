from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
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
