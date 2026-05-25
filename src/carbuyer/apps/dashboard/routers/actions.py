from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
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
from carbuyer.apps.dashboard.routers.watched import build_watchlist_buckets
from carbuyer.db.enums import UserAction, ValuationStatus
from carbuyer.db.lot_state import apply_user_action
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import notify
from carbuyer.shared.logging import get_logger

router = APIRouter()
log = get_logger("dashboard.actions")


@router.post("/lots/{lot_id}/mark", response_model=None)
async def mark_lot(
    request: Request,
    lot_id: int,
    action: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    currently_active: Annotated[bool, Form()] = False,
    max_bid_cad: Annotated[Decimal | None, Form()] = None,
) -> HTMLResponse | Response:
    """Set, toggle, or clear `user_action` for a lot via apply_user_action.

    `action` is the button intent ("interested" / "bid_placed" / "purchased"
    / "passed"). `currently_active=True` treats the click as toggle-off
    (clear to NULL). `max_bid_cad` is REQUIRED when action == "bid_placed"
    and `currently_active` is False.
    """
    if action not in {"interested", "bid_placed", "purchased", "passed"}:
        raise HTTPException(status_code=422, detail=f"invalid action {action!r}")

    if action == "bid_placed" and max_bid_cad is None and not currently_active:
        raise HTTPException(
            status_code=422,
            detail="bid_placed requires max_bid_cad",
        )

    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)

    target: UserAction | None = None if currently_active else UserAction(action)

    apply_user_action(
        session, lot, target,
        max_bid_cad=max_bid_cad,
        source="dashboard",
    )
    await session.commit()
    await session.refresh(lot)

    log.info(
        "lot marked", lot_id=lot_id, action=action,
        stored=lot.user_action, toggled_off=currently_active,
    )

    effective_state = lot.user_action.value if lot.user_action else None

    if not request.headers.get("HX-Request"):
        return Response(status_code=204)

    include_modal_oob_clear = (
        request.headers.get("HX-Request") == "true"
    )
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
                "include_modal_oob_clear": include_modal_oob_clear,
            },
        )

    if hx_target == "watchlist-board":
        buckets = await build_watchlist_buckets(session)
        return templates.TemplateResponse(
            request,
            "partials/watchlist_board.html",
            {"buckets": buckets, "include_modal_oob_clear": include_modal_oob_clear},
        )

    auction = await session.get(Auction, lot.auction_id)
    return templates.TemplateResponse(
        request,
        "partials/lot_card.html",
        {
            "item": {"lot": lot, "auction": auction},
            "effective_state": effective_state,
            "include_modal_oob_clear": include_modal_oob_clear,
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


@router.get("/lots/{lot_id}/bid-modal", response_class=HTMLResponse)
async def bid_modal(
    request: Request,
    lot_id: int,
    return_target: Annotated[str, Query(min_length=1)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> HTMLResponse:
    """Render the place-bid modal. `return_target` is the element id
    (without leading '#') that the form should swap on submit — the
    caller (action_buttons macro) computes it from its own hx-target so
    the modal posts back into the right region (card / board / fragment).
    """
    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/bid_modal.html",
        {"lot": lot, "return_target": return_target},
    )


@router.get("/modal/dismiss", response_class=HTMLResponse)
async def modal_dismiss() -> HTMLResponse:
    """Empty body. Cancel + backdrop swap this into #modal-slot."""
    return HTMLResponse("")
