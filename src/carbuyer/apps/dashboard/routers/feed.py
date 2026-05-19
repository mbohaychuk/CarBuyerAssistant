"""Feed router — the homepage triage view.

Supports:
  - free-text search (`q`) via ILIKE against title/make/model
  - sort options ("deal" | "closing" | "newest" | "rarity")
  - preset chips that pre-load filter combinations
  - province multi-select (now as toggle-chip checkboxes)
  - cursor-based infinite scroll keyed on (sort_key, id)

Preset chips set both `preset` and the filter params they imply; the
server interprets the canonical preset name so the URL stays clean.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import OPEN_STATUSES, get_session, is_htmx
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot

router = APIRouter()

# Spec §6: blended score = 0.5 × rarity + 0.5 × price_deal_score, used as
# the default sort. Each weight is independently configurable later;
# pinned here so it stays visible at the query callsite (changes
# invalidate cached cursors).
_RARITY_WEIGHT = 0.5
_PRICE_WEIGHT = 0.5

_SortKey = Literal["deal", "closing", "newest", "rarity"]

# Preset chips translate to filter overrides applied server-side. Keeping
# the canonical names in one dict makes the chip list in the template
# match the router without manual sync.
_PRESETS: dict[str, dict[str, Any]] = {
    "closing_24h":  {"sort": "closing", "closing_hours": 24},
    "under_10k":    {"max_price_cad": 10000},
    "deal_25":      {"min_score": 0.25, "sort": "deal"},
    "watched":      {"watched_only": True},
    "no_showstop":  {"hide_showstoppers": True},
    "rare":         {"min_rarity": 3.0, "sort": "rarity"},
}


def _build_active_chips(
    *, province: list[str], q: str | None, min_score: float, min_rarity: float,
    sort: _SortKey, preset: str | None, max_price_cad: int | None,
) -> list[dict[str, str]]:
    """Surface the filters currently in effect as removable chips.

    Each chip has a `label` (what the user sees) and a `remove_url` (the
    same querystring with this single filter dropped). The empty list is
    rendered as no chip row — clean canvas when no filters are active.
    """
    chips: list[dict[str, str]] = []
    if q:
        chips.append({"label": f'"{q}"', "remove_url": "/"})
    if province:
        chips.append({"label": " · ".join(province), "remove_url": "/"})
    if min_score > 0:
        chips.append({
            "label": f"{int(min_score * 100)}%+ under",
            "remove_url": "/",
        })
    if min_rarity > 0:
        chips.append({
            "label": f"Rarity ≥ {min_rarity:g}",
            "remove_url": "/",
        })
    if max_price_cad:
        chips.append({
            "label": f"Under ${max_price_cad:,}",
            "remove_url": "/",
        })
    if preset:
        chips.append({"label": f"Preset: {preset}", "remove_url": "/"})
    return chips


@router.get("/", response_class=HTMLResponse)
async def feed(  # noqa: PLR0912, PLR0913, PLR0915
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    province: Annotated[list[str] | None, Query()] = None,
    q: str | None = None,
    sort: _SortKey = "deal",
    preset: str | None = None,
    min_score: float = 0.0,
    min_rarity: float = 0.0,
    max_price_cad: int | None = None,
    closing_hours: int | None = None,
    watched_only: bool = False,
    hide_showstoppers: bool = False,
    exclude_not_interested: bool = True,
    cursor: int | None = None,
    cursor_score: float | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> HTMLResponse:
    # Preset chips override the explicit filter params. Done first so any
    # query-string filters layered on top still take precedence (a user
    # can click "25%+ under" then add province=AB without losing either).
    if preset and preset in _PRESETS:
        for key, value in _PRESETS[preset].items():
            if key == "sort":
                sort = value
            elif key == "closing_hours":
                closing_hours = value
            elif key == "max_price_cad":
                max_price_cad = value
            elif key == "min_score":
                min_score = max(min_score, value)
            elif key == "min_rarity":
                min_rarity = max(min_rarity, value)
            elif key == "watched_only":
                watched_only = True
            elif key == "hide_showstoppers":
                hide_showstoppers = True

    blended = (
        _PRICE_WEIGHT * func.coalesce(AuctionLot.price_deal_score, 0.0)
        + _RARITY_WEIGHT * func.coalesce(AuctionLot.rarity_score, 0.0)
    ).label("blended_score")

    stmt = (
        select(AuctionLot, Auction, blended)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.lot_status.in_(OPEN_STATUSES),
            # Non-vehicle accessories slip through HiBid's category 700006
            # with no year/make/model. Hide from the feed. (Watchlist sees
            # them — a user who marked Interested wants visibility either way.)
            AuctionLot.year.is_not(None),
        )
    )
    if province:
        stmt = stmt.where(Auction.pickup_province.in_(province))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                AuctionLot.title.ilike(like),
                AuctionLot.make.ilike(like),
                AuctionLot.model.ilike(like),
                AuctionLot.trim.ilike(like),
                AuctionLot.vin.ilike(like),
            ),
        )
    if min_score > 0:
        stmt = stmt.where(AuctionLot.price_deal_score >= min_score)
    if min_rarity > 0:
        stmt = stmt.where(AuctionLot.rarity_score >= min_rarity)
    if max_price_cad:
        stmt = stmt.where(AuctionLot.current_high_bid_cad <= max_price_cad)
    if closing_hours:
        cutoff = datetime.now(UTC) + timedelta(hours=closing_hours)
        stmt = stmt.where(
            or_(
                Auction.scheduled_end_at <= cutoff,
                AuctionLot.scheduled_end_at <= cutoff,
            ),
        )
    if watched_only:
        stmt = stmt.where(
            AuctionLot.user_action.in_(
                [UserAction.INTERESTED.value, UserAction.MAYBE.value],
            ),
        )
    elif exclude_not_interested:
        stmt = stmt.where(
            AuctionLot.user_action.is_distinct_from(UserAction.NOT_INTERESTED.value),
        )
    if hide_showstoppers:
        stmt = stmt.where(
            func.jsonb_array_length(AuctionLot.showstopper_flags) == 0,
        )

    # Composite cursor: blended (default sort) uses (score, id); other
    # sorts use (sort_column, id). cursor_score is passed in the next-page
    # URL so the cursor doesn't drift if a re-rank happens between requests.
    if sort == "closing":
        effective_end = func.coalesce(
            Auction.scheduled_end_at, AuctionLot.scheduled_end_at,
        )
        if cursor is not None and cursor_score is not None:
            stmt = stmt.where(
                or_(
                    effective_end > datetime.fromtimestamp(cursor_score, UTC),
                    and_(
                        effective_end == datetime.fromtimestamp(cursor_score, UTC),
                        AuctionLot.id > cursor,
                    ),
                ),
            )
        elif cursor is not None:
            stmt = stmt.where(AuctionLot.id > cursor)
        stmt = stmt.order_by(effective_end.asc().nulls_last(), AuctionLot.id.asc())
    elif sort == "newest":
        if cursor is not None:
            stmt = stmt.where(AuctionLot.id < cursor)
        stmt = stmt.order_by(AuctionLot.id.desc())
    elif sort == "rarity":
        if cursor is not None and cursor_score is not None:
            stmt = stmt.where(
                or_(
                    AuctionLot.rarity_score < cursor_score,
                    and_(
                        AuctionLot.rarity_score == cursor_score,
                        AuctionLot.id < cursor,
                    ),
                ),
            )
        elif cursor is not None:
            stmt = stmt.where(AuctionLot.id < cursor)
        stmt = stmt.order_by(
            func.coalesce(AuctionLot.rarity_score, 0.0).desc(),
            AuctionLot.id.desc(),
        )
    else:  # "deal" — the default; uses the blended score
        if cursor is not None and cursor_score is not None:
            stmt = stmt.where(
                or_(
                    blended < cursor_score,
                    and_(blended == cursor_score, AuctionLot.id < cursor),
                ),
            )
        elif cursor is not None:
            stmt = stmt.where(AuctionLot.id < cursor)
        stmt = stmt.order_by(blended.desc(), AuctionLot.id.desc())

    stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).all()
    items: list[dict[str, Any]] = [
        {"lot": lot, "auction": auction} for (lot, auction, _score) in rows
    ]
    if rows:
        last_lot, last_auction, last_score = rows[-1]
        next_cursor = last_lot.id
        if sort == "closing":
            effective = last_auction.scheduled_end_at or last_lot.scheduled_end_at
            next_cursor_score = effective.timestamp() if effective else None
        elif sort == "rarity":
            next_cursor_score = (
                float(last_lot.rarity_score) if last_lot.rarity_score is not None else 0.0
            )
        elif sort == "deal":
            next_cursor_score = float(last_score) if last_score is not None else 0.0
        else:
            next_cursor_score = None
    else:
        next_cursor = None
        next_cursor_score = None

    active_chips = _build_active_chips(
        province=province or [], q=q,
        min_score=min_score, min_rarity=min_rarity, sort=sort,
        preset=preset, max_price_cad=max_price_cad,
    )

    template = "partials/lot_list.html" if is_htmx(request) else "pages/feed.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "items": items,
            "next_cursor": next_cursor,
            "next_cursor_score": next_cursor_score,
            "filters": {
                "province": province or [],
                "q": q or "",
                "sort": sort,
                "preset": preset,
                "min_score": min_score,
                "min_rarity": min_rarity,
                "max_price_cad": max_price_cad,
                "exclude_not_interested": exclude_not_interested,
                "active_chips": active_chips,
            },
        },
    )
