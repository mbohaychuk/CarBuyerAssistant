"""Auctions section — grouped-by-event view.

Stub for the nav slot until the full Auctions page lands in a later PR.
Today the closest existing surface is the closing-soon view; this stub
points users there. Once the real Auctions page is built it'll replace
this module entirely.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from carbuyer.apps.dashboard.app import templates

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
