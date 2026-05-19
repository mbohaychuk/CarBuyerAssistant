from __future__ import annotations

from typing import Annotated

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
# state names without breaking. Map "passed" to the existing
# "not_interested" value; "bid_placed" / "purchased" alias to
# "interested" in the DB (the workflow migration replaces this with
# distinct enum values).
_ACCEPTED_ACTIONS = frozenset({
    "interested", "maybe", "not_interested",
    "bid_placed", "purchased", "passed",
})


def _to_enum_value(action: str) -> str:
    if action == "passed":
        return UserAction.NOT_INTERESTED.value
    if action in {"bid_placed", "purchased"}:
        return UserAction.INTERESTED.value  # transitional alias
    return UserAction(action).value


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
    db_value = _to_enum_value(action)
    lot.user_action = db_value
    await session.commit()
    await session.refresh(lot)
    log.info("lot marked", lot_id=lot_id, action=action, stored=db_value)

    if not request.headers.get("HX-Request"):
        return Response(status_code=204)

    # The forward-looking action name (e.g. "bid_placed") survives in the
    # rendered output via `effective_state`, so the button the user
    # actually clicked appears active even though the DB stores the
    # aliased value. A subsequent full page load will revert to the
    # stored value — known transitional limitation until the workflow
    # migration ships, documented in [[dashboard-redesign-direction-a]].
    hx_target = request.headers.get("HX-Target", "") or ""
    is_button_fragment_target = (
        hx_target.endswith("-desktop") or hx_target.endswith("-mobile")
    )
    if is_button_fragment_target:
        wrapper_class = (
            "decision-card__actions" if hx_target.endswith("-desktop")
            else "bid-console__actions"
        )
        return templates.TemplateResponse(
            request,
            "partials/action_buttons_fragment.html",
            {
                "lot_id": lot.id,
                "target_id": hx_target,
                "wrapper_class": wrapper_class,
                "effective_state": action,
            },
        )
    auction = await session.get(Auction, lot.auction_id)
    return templates.TemplateResponse(
        request,
        "partials/lot_card.html",
        {
            "item": {"lot": lot, "auction": auction},
            "effective_state": action,
        },
    )


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
