from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import CurrentUser, get_session, require_admin
from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import notify
from carbuyer.shared.logging import get_logger
from carbuyer.sources.resolver import resolve_auction_url

router = APIRouter()
log = get_logger("dashboard.needs_plugin")

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
    _admin: Annotated[CurrentUser, Depends(require_admin)],
) -> Response:
    auction = await session.get(Auction, auction_id)
    if auction is None:
        raise HTTPException(status_code=404)
    old_source = auction.source
    # resolve_auction_url walks every registered plugin's parse_auction_url
    # and returns the first AuctionRef whose source recognises the URL.
    # None means no plugin matches today -- skip the re-route silently.
    # (Previously this branched on a separate "unknown:<host>" return shape
    # from FAG's resolve_platform; consolidating into one None path is the
    # same operator outcome with less code.)
    ref = resolve_auction_url(auction.url)
    if ref is None:
        log.warning(
            "retry_routing skipped: no plugin matches url",
            auction_id=auction_id, url=auction.url,
        )
        return Response(status_code=204)
    new_source = ref.source
    new_ext_id = ref.source_auction_id

    auction.source = new_source
    auction.source_auction_id = new_ext_id
    now = datetime.now(UTC)
    auction.routing_resolved_at = now
    # Stamp the alert timestamp too: the dashboard action that resolves this
    # state is itself an acknowledgement, even if no Discord post fired. Keeps
    # the column's invariant (NULL = unresolved) honest.
    if auction.needs_plugin_notified_at is None:
        auction.needs_plugin_notified_at = now

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
    try:
        await session.commit()
    except IntegrityError:
        # Another row already exists with (new_source, new_ext_id) — most
        # commonly because the ingester surfaced the same auction via a
        # plugin in parallel. The unknown:* row is now a stale duplicate;
        # we leave it for ops to clean up rather than risk merging lots across
        # a unique-constraint boundary.
        await session.rollback()
        log.warning(
            "retry_routing collision: target source already exists",
            auction_id=auction_id,
            old_source=old_source,
            new_source=new_source,
            new_source_auction_id=new_ext_id,
        )
        raise HTTPException(
            status_code=409,
            detail="auction already exists under target source",
        ) from None
    log.info(
        "auction re-routed",
        auction_id=auction_id,
        old_source=old_source,
        new_source=new_source,
        new_source_auction_id=new_ext_id,
    )
    return Response(status_code=204)
