"""Today inbox — the morning-triage homepage.

GET / renders four sections:
  1. KPI strip (closing-now count, watching count, alerts count, best-deal %)
  2. "Since your last visit" — three alert categories
  3. Closing buckets: Now / Next 2h / Today / Tomorrow
  4. Best deals — top-N open lots by deal score

Reads `dashboard_state.last_visited_at`, computes alerts against that
watermark, then bumps the watermark to now() — so the next page load
shows a fresh delta. Aggregator queries live in `today_queries.py`;
this module is just routing + render.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.apps.dashboard.today_queries import (
    alerts_since,
    best_deals,
    closing_buckets,
    dashboard_kpis,
    read_and_bump_last_visit,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def today(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    now = datetime.now(UTC)

    # Read the watermark first, then bump it. The bump commits when the
    # request completes, so a concurrent reload mid-render reads the
    # same prev value (until the first commit lands).
    prev_visit = await read_and_bump_last_visit(session)

    alerts = await alerts_since(session, since=prev_visit)
    buckets = await closing_buckets(session, now=now)
    deals = await best_deals(session)
    kpis = await dashboard_kpis(session, now=now, alerts_total=alerts.total)

    await session.commit()

    return templates.TemplateResponse(
        request,
        "pages/today.html",
        {
            "kpis": kpis,
            "alerts": alerts,
            "buckets": buckets,
            "best_deals": deals,
            "prev_visit": prev_visit,
            "now": now,
        },
    )
