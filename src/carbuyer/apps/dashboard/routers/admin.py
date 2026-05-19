"""Admin section — consolidated needs-plugin + health + ingest history.

Stub for the nav slot until the full Admin page lands in a later PR.
Today the existing /needs-plugin and /health surfaces serve the same
operational role.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from carbuyer.apps.dashboard.app import templates

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
async def admin_placeholder(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "pages/_placeholder.html",
        {
            "title": "Admin",
            "nav": "admin",
            "blurb": (
                "Coming soon: consolidated pipeline health, per-source "
                "ingest history, and needs-plugin triage in one place."
            ),
            "fallback_label": "Needs plugin (current closest view)",
            "fallback_url": "/needs-plugin",
            "fallback_label_2": "Pipeline health",
            "fallback_url_2": "/health",
        },
    )
