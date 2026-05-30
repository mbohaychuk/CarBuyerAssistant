"""Auctions section — grouped-by-event view.

Stub for the nav slot until the full Auctions page lands in a later PR.
Today the closest existing surface is the closing-soon view; this stub
points users there. Once the real Auctions page is built it'll replace
this module entirely.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.auction_digest.runner import (
    _build_sections,  # pyright: ignore[reportPrivateUsage]
)
from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import Auction

router = APIRouter()


@router.get("/auctions", response_class=HTMLResponse)
async def auctions_placeholder(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "pages/_placeholder.html",
        {
            "title": "Auctions",
            "nav": "auctions",
            "blurb": (
                "Coming soon: grouped-by-event view with location, buyer "
                "premium, and total recommended-bid envelope per auction."
            ),
            "fallback_label": "Closing soon (current closest view)",
            "fallback_url": "/closing",
        },
    )


@router.get("/auctions/{auction_id}/digest", response_class=HTMLResponse)
async def auction_digest_preview(
    request: Request,
    auction_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    auction = await session.get(Auction, auction_id)
    if auction is None:
        return Response("Not found", status_code=404)
    matches, rare = await _build_sections(session, auction)
    return templates.TemplateResponse(
        request,
        "pages/auction_digest_preview.html",
        {"auction": auction, "matches": matches, "rare": rare},
    )
