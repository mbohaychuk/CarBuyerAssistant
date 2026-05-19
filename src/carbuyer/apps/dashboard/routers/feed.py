"""Feed router — the filter-heavy "browse all lots" view at `/lots`.

(Used to be the homepage at `/`; replaced by the Today inbox in the
inbox redesign. The filter UX is still load-bearing for power-use
browsing, so it lives on at `/lots`.)

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
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import OPEN_STATUSES, get_session, is_htmx
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.shared.logging import get_logger

router = APIRouter()
log = get_logger("dashboard.feed")

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

# Keys we strip from a query-params dict when computing a "with one
# filter removed" URL — these aren't filters and shouldn't be touched
# when an active-filter chip's × is clicked.
_NON_FILTER_PARAMS = frozenset({"cursor", "cursor_score"})


def _query_url(params: dict[str, Any], *, drop: set[str] | None = None) -> str:
    """Render a `/lots` URL from the current params, optionally dropping keys.

    Used for active-filter chip × buttons (drop the one filter) and for
    preset chips (apply preset on top of current state). Multi-value keys
    (province) are encoded as repeated params.
    """
    drop = drop or set()
    clean: list[tuple[str, str]] = []
    for key, value in params.items():
        if key in drop or key in _NON_FILTER_PARAMS:
            continue
        if value is None or value == "" or value == 0 or value == 0.0:
            continue
        if isinstance(value, list):
            clean.extend((key, str(v)) for v in value)
        elif isinstance(value, bool):
            if value:
                clean.append((key, "true"))
        else:
            clean.append((key, str(value)))
    return "/lots" + ("?" + urlencode(clean) if clean else "")


def _build_active_chips(
    *, all_params: dict[str, Any], province: list[str], q: str | None,
    min_score: float, min_rarity: float, preset: str | None,
    max_price_cad: int | None,
) -> list[dict[str, str]]:
    """Active-filter summary chips. Each chip's remove_url drops only
    that one filter from the current querystring, preserving the rest.
    """
    chips: list[dict[str, str]] = []
    if q:
        chips.append({"label": f'"{q}"', "remove_url": _query_url(all_params, drop={"q"})})
    if province:
        chips.append({
            "label": " · ".join(province),
            "remove_url": _query_url(all_params, drop={"province"}),
        })
    if min_score > 0:
        chips.append({
            "label": f"{int(min_score * 100)}%+ under",
            "remove_url": _query_url(all_params, drop={"min_score"}),
        })
    if min_rarity > 0:
        chips.append({
            "label": f"Rarity ≥ {min_rarity:g}",
            "remove_url": _query_url(all_params, drop={"min_rarity"}),
        })
    if max_price_cad:
        chips.append({
            "label": f"Under ${max_price_cad:,}",
            "remove_url": _query_url(all_params, drop={"max_price_cad"}),
        })
    if preset:
        chips.append({
            "label": f"Preset: {preset}",
            "remove_url": _query_url(all_params, drop={"preset"}),
        })
    return chips


@router.get("/lots", response_class=HTMLResponse)
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
    # Track the params the user actually sent (before preset expansion)
    # so we can render active-filter chips + preset chips + pagination
    # URLs that preserve user intent. The expanded values are used only
    # internally for the query.
    raw_params: dict[str, Any] = {
        "province": province or [],
        "q": q or "",
        "sort": sort,
        "preset": preset,
        "min_score": min_score,
        "min_rarity": min_rarity,
        "max_price_cad": max_price_cad,
        "closing_hours": closing_hours,
        "watched_only": watched_only,
        "hide_showstoppers": hide_showstoppers,
        "exclude_not_interested": exclude_not_interested,
    }

    # Apply preset overrides. Numeric thresholds widen with max() so an
    # explicit user param can never be silently weakened by a preset.
    # Other keys (sort, closing_hours, max_price_cad) are set only when
    # the user did NOT already specify them — preset shouldn't clobber
    # an explicit choice.
    if preset:
        if preset not in _PRESETS:
            log.warning("unknown preset requested; ignored", preset=preset)
            preset = None  # drop from active chips
            raw_params["preset"] = None
        else:
            for key, value in _PRESETS[preset].items():
                if key == "sort" and sort == "deal":  # only override default
                    sort = value
                elif key == "closing_hours" and closing_hours is None:
                    closing_hours = value
                elif key == "max_price_cad" and max_price_cad is None:
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
        # Exclude NULL effective_end entirely for this sort — otherwise
        # the cursor predicate `effective_end > cursor` evaluates NULL>x
        # as NULL/false and the NULL tail becomes unreachable. Lots
        # without an end time aren't candidates for closing-soonest anyway.
        effective_end = func.coalesce(
            Auction.scheduled_end_at, AuctionLot.scheduled_end_at,
        )
        stmt = stmt.where(effective_end.is_not(None))
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
        stmt = stmt.order_by(effective_end.asc(), AuctionLot.id.asc())
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
        all_params=raw_params,
        province=province or [], q=q,
        min_score=min_score, min_rarity=min_rarity,
        preset=preset, max_price_cad=max_price_cad,
    )

    # Pre-build a cursor URL that carries every active filter param.
    # Pagination template uses this verbatim; lot_list.html no longer
    # has to know about the filter shape.
    pagination_params = dict(raw_params)
    if next_cursor is not None:
        pagination_params["cursor"] = next_cursor
        if next_cursor_score is not None:
            pagination_params["cursor_score"] = next_cursor_score
        pagination_url = _query_url(pagination_params)
    else:
        pagination_url = None

    # Each preset chip's URL: current filters + preset=<key>. Clicking a
    # second preset replaces, not stacks (presets are mutually exclusive
    # by design). Active chip rendering already toggles via data-active.
    preset_chip_urls = {
        key: _query_url({**raw_params, "preset": key, "cursor": None, "cursor_score": None})
        for key in _PRESETS
    }

    template = "partials/lot_list.html" if is_htmx(request) else "pages/feed.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "items": items,
            "next_cursor": next_cursor,
            "next_cursor_score": next_cursor_score,
            "pagination_url": pagination_url,
            "preset_chip_urls": preset_chip_urls,
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
