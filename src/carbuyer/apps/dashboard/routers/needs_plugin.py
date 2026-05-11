from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import notify
from carbuyer.sources.farmauctionguide.source import resolve_platform

router = APIRouter()

_LIMIT = 200


@router.get("/needs-plugin", response_class=HTMLResponse)
async def needs_plugin_view(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    stmt = (
        select(Auction)
        .where(Auction.source.like("unknown:%"))
        .order_by(
            Auction.scheduled_start_at.asc().nulls_last(),
            Auction.first_seen_at.asc(),
        )
        .limit(_LIMIT)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    now = datetime.now(UTC)
    return templates.TemplateResponse(
        request,
        "pages/needs_plugin.html",
        {"rows": rows, "now": now},
    )


@router.post("/admin/auctions/{auction_id}/retry_routing", status_code=204)
async def retry_routing(
    auction_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    auction = await session.get(Auction, auction_id)
    if auction is None:
        raise HTTPException(status_code=404)
    resolved = resolve_platform(auction.url)
    if resolved is None:
        # Known host but URL has no auction-id — the link was a footer/nav/help
        # page that should never have been routed; nothing to do.
        return Response(status_code=204)
    new_source, new_ext_id = resolved
    if new_source.startswith("unknown:"):
        # No plugin matches yet (still routes to an unknown:<host> bucket).
        return Response(status_code=204)

    auction.source = new_source
    auction.source_auction_id = new_ext_id
    auction.routing_resolved_at = datetime.now(UTC)

    # Reset any lots already associated so they re-process under the new source.
    # In practice there should be zero lots since the source was unknown, but
    # being explicit is cheap insurance against edge cases.
    await session.execute(
        update(AuctionLot)
        .where(AuctionLot.auction_id == auction.id)
        .values(
            enrichment_status=EnrichmentStatus.PENDING.value,
            valuation_status=ValuationStatus.PENDING.value,
            vision_status=VisionStatus.PENDING.value,
            notification_status=NotificationStatus.PENDING.value,
        ),
    )
    await notify(session, "auction_pending", str(auction.id))
    await session.commit()
    return Response(status_code=204)
