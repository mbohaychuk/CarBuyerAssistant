from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import (
    CurrentUser,
    current_user,
    get_session,
    require_admin,
)
from carbuyer.db.enums import UserAction, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import notify
from carbuyer.shared.logging import get_logger

router = APIRouter()
log = get_logger("dashboard.actions")


# The 4-state workflow (interested → bid_placed → purchased → passed) is
# in flight as a separate migration. Until those values land in the
# UserAction enum, accept the new names alongside the existing 3-value
# set so the redesigned LotCard's action buttons can post forward-looking
# state names without breaking. Map legacy "not_interested" form value to
# the canonical "passed" name once the enum migrates.
_ACCEPTED_ACTIONS = frozenset({
    "interested", "maybe", "not_interested",
    "bid_placed", "purchased", "passed",
})


@router.post("/lots/{lot_id}/mark", response_model=None)
async def mark_lot(
    request: Request,
    lot_id: int,
    action: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> HTMLResponse | Response:
    if action not in _ACCEPTED_ACTIONS:
        raise HTTPException(status_code=422, detail=f"invalid action {action!r}")
    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)
    # Translate forward-looking state names that aren't in the enum yet
    # to their nearest existing-enum equivalent. Once the 4-state
    # migration ships these become enum values directly.
    db_value = action
    if action == "passed":
        db_value = UserAction.NOT_INTERESTED.value
    elif action == "bid_placed":
        db_value = UserAction.INTERESTED.value  # transitional alias
    elif action == "purchased":
        db_value = UserAction.INTERESTED.value  # transitional alias
    lot.user_action = db_value
    await session.commit()
    await session.refresh(lot)
    log.info("lot marked", lot_id=lot_id, action=action, stored=db_value)

    # HTMX caller gets the refreshed lot card HTML so the page can swap
    # outerHTML without a full reload. The data-state attribute on the
    # card root drives the visual state (left-border color, active
    # button highlight). Non-HTMX callers get 204 (legacy contract for
    # the JS-less form path).
    if request.headers.get("HX-Request"):
        auction = await session.get(Auction, lot.auction_id)
        return templates.TemplateResponse(
            request,
            "partials/lot_card.html",
            {"item": {"lot": lot, "auction": auction}},
        )
    return Response(status_code=204)


@router.post("/lots/{lot_id}/notes", status_code=204)
async def append_note(
    lot_id: int,
    note: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)
    existing = lot.notes or ""
    lot.notes = (existing + "\n" + note).strip() if existing else note
    await session.commit()
    log.info("note appended", lot_id=lot_id, note_len=len(note))
    return Response(status_code=204)


@router.post("/admin/rescore", status_code=204)
async def rescore_all(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[CurrentUser, Depends(require_admin)],
) -> Response:
    await session.execute(
        update(AuctionLot).values(valuation_status=ValuationStatus.PENDING.value),
    )
    # Bulk wake-up: valuator's catchup sweep won't run until next restart,
    # and a single NOTIFY drains every pending row (payload ignored).
    await notify(session, "valuation_pending", "")
    await session.commit()
    log.info("rescore triggered")
    return Response(status_code=204)
