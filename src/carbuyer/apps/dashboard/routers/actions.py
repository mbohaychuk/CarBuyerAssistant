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


_ACCEPTED_ACTIONS = frozenset({"interested", "bid_placed", "purchased", "passed"})


@router.post("/lots/{lot_id}/mark", response_model=None)
async def mark_lot(
    request: Request,
    lot_id: int,
    action: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    currently_active: Annotated[bool, Form()] = False,
) -> HTMLResponse | Response:
    """Set, toggle, or clear `user_action` for a lot.

    `action` is the button's intent ("interested" / "bid_placed" /
    "passed" / etc). `currently_active` is the button's own
    `data-active` value — the macro passes it through via `hx-vals` so
    the server can treat "click an already-active button" as toggle-off
    (clearing user_action to NULL). Without this, clicking Watch on an
    already-watched lot just re-writes the same value, leaving no way
    to un-watch without explicitly clicking Pass.
    """
    if action not in _ACCEPTED_ACTIONS:
        raise HTTPException(status_code=422, detail=f"invalid action {action!r}")
    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)
    if currently_active:
        lot.user_action = None
        effective_state: str | None = None
    else:
        lot.user_action = UserAction(action)
        effective_state = action
    await session.commit()
    await session.refresh(lot)
    log.info(
        "lot marked", lot_id=lot_id, action=action,
        stored=lot.user_action, toggled_off=currently_active,
    )

    if not request.headers.get("HX-Request"):
        return Response(status_code=204)

    # On a toggle-off, effective_state is None so no button renders active.
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
                "effective_state": effective_state,
            },
        )
    auction = await session.get(Auction, lot.auction_id)
    return templates.TemplateResponse(
        request,
        "partials/lot_card.html",
        {
            "item": {"lot": lot, "auction": auction},
            "effective_state": effective_state,
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
