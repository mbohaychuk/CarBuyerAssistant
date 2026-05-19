"""Aggregator queries for the Today inbox (`GET /`).

Five independent reads against `auction_lots` (+ `auctions` join) that
fill the four template regions: KPI strip, alerts-since-last-visit,
closing buckets, best deals. Kept in one module — not a `repository.py`
ceremony — because each query is small and tightly coupled to the
template that consumes it.

The day boundary for "today" / "tomorrow" is computed in America/Edmonton
(Western Canada). Single-user dev tool — if we ever go multi-user we'd
move this to a user preference, but hardcoding here keeps the v1 simple.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.deps import OPEN_STATUSES
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot, DashboardState

_LOCAL_TZ = ZoneInfo("America/Edmonton")

# Boundary between "Now" and "Next 2h" buckets. Lots within this window
# are treated as actively-closing — surfaced as compact dense rows.
_NOW_WINDOW = timedelta(minutes=15)
_NEXT_WINDOW = timedelta(hours=2)


@dataclass(slots=True)
class TodayKPIs:
    """The four numeric tiles above the fold.

    - closing_now: lots whose effective_end is within ~15 min
    - watching:    lots the user marked Interested (current 3-state schema)
    - alerts:      sum of the three alert categories (passed in by caller
                   because alerts_since already computes its own counts)
    - best_deal_pct: top deal-score % among open biddable lots (None if
                   the inventory has no scored open lots yet)
    """

    closing_now: int
    watching: int
    alerts: int
    best_deal_pct: float | None


@dataclass(slots=True)
class AlertsBundle:
    """Three alert lists, each a list of {lot, auction, reason} dicts.

    The route flattens these into a count for the KPI tile and renders
    them grouped in the alerts panel. Each list is independently capped
    at 8 entries to keep the section scannable.
    """

    new_lots: list[dict[str, Any]] = field(default_factory=list)
    state_transitions: list[dict[str, Any]] = field(default_factory=list)
    showstoppers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.new_lots) + len(self.state_transitions) + len(self.showstoppers)


@dataclass(slots=True)
class ClosingBuckets:
    """Four time-bin lists of {lot, auction} items.

    Bucket boundaries:
      now      : effective_end ≤ now+15 min
      next_2h  : now+15 min < effective_end ≤ now+2h
      today    : now+2h < effective_end ≤ end-of-today-local
      tomorrow : end-of-today-local < effective_end ≤ end-of-tomorrow-local
    """

    now: list[dict[str, Any]] = field(default_factory=list)
    next_2h: list[dict[str, Any]] = field(default_factory=list)
    today: list[dict[str, Any]] = field(default_factory=list)
    tomorrow: list[dict[str, Any]] = field(default_factory=list)


# ── public surface ──────────────────────────────────────────────────────


async def read_and_bump_last_visit(session: AsyncSession) -> datetime:
    """Atomically read the prior `last_visited_at` and update it to now().

    Returns the *previous* value so the route can use it as the
    "alerts since" lower bound. Uses a RETURNING-style update so the
    read/write happen in one statement — no race window between two
    overlapping tab refreshes.

    The singleton row is guaranteed to exist (seeded by migration), so
    this never has to handle the missing-row case.
    """
    # Read the OLD value, then bump. Two statements inside the same
    # transaction — concurrent readers see either before-both or
    # after-both under Postgres' default READ COMMITTED.
    previous_visit = (
        await session.execute(
            select(DashboardState.last_visited_at).where(DashboardState.id == 1),
        )
    ).scalar_one()
    await session.execute(
        update(DashboardState)
        .where(DashboardState.id == 1)
        .values(last_visited_at=datetime.now(UTC)),
    )
    return previous_visit


async def derive_watched_make_model(session: AsyncSession) -> set[tuple[str, str]]:
    """The (make, model) pairs the user has shown interest in.

    Auto-derived from the user's Interested history rather than requiring
    a separate watch-config table. Both make and model must be non-null —
    accessory lots (no normalized fields) can't be matched against.
    """
    stmt = (
        select(AuctionLot.make, AuctionLot.model)
        .where(
            AuctionLot.user_action == UserAction.INTERESTED.value,
            AuctionLot.make.is_not(None),
            AuctionLot.model.is_not(None),
        )
        .distinct()
    )
    rows = (await session.execute(stmt)).all()
    return {(make, model) for make, model in rows}


async def alerts_since(
    session: AsyncSession, *, since: datetime, limit_per_section: int = 8,
) -> AlertsBundle:
    """The three alert categories since `since`.

    A: new lots ingested matching the user's watched make/model set
    B: price moved on a lot the user marked Interested (last_bid_observed_at
       crossed the watermark)
    C: a Interested lot got a showstopper flag late (updated_at after
       last visit AND non-empty showstopper_flags)

    Each section ordered most-recent-first and capped to keep the panel
    scannable. The route may surface a "+N more" hint if a section caps.
    """
    bundle = AlertsBundle()
    watched = await derive_watched_make_model(session)

    # A — new lots in watched (make, model). Empty watched set → skip
    # entirely (avoids an `IN ()` SQL error and is the correct semantics:
    # no watched models = no new-lot alerts possible).
    if watched:
        new_stmt = (
            select(AuctionLot, Auction)
            .join(Auction, Auction.id == AuctionLot.auction_id)
            .where(
                AuctionLot.created_at > since,
                AuctionLot.lot_status.in_(OPEN_STATUSES),
                tuple_(AuctionLot.make, AuctionLot.model).in_(watched),
            )
            .order_by(AuctionLot.created_at.desc())
            .limit(limit_per_section)
        )
        for lot, auction in (await session.execute(new_stmt)).all():
            bundle.new_lots.append({"lot": lot, "auction": auction, "reason": "new"})

    # B — price moved on an interested lot since last visit. We use
    # last_bid_observed_at as the watermark; lots that haven't seen a
    # bid since the user looked don't fire.
    moved_stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.user_action == UserAction.INTERESTED.value,
            AuctionLot.lot_status.in_(OPEN_STATUSES),
            AuctionLot.last_bid_observed_at.is_not(None),
            AuctionLot.last_bid_observed_at > since,
        )
        .order_by(AuctionLot.last_bid_observed_at.desc())
        .limit(limit_per_section)
    )
    for lot, auction in (await session.execute(moved_stmt)).all():
        bundle.state_transitions.append(
            {"lot": lot, "auction": auction, "reason": "bid_moved"},
        )

    # C — late-arriving showstoppers on interested lots. The enricher /
    # vision-batcher can write new flags days after first ingest;
    # updated_at > since AND non-empty array catches the case where a
    # flag appeared after the user marked Interested.
    ss_stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.user_action == UserAction.INTERESTED.value,
            AuctionLot.updated_at > since,
            func.jsonb_array_length(AuctionLot.showstopper_flags) > 0,
        )
        .order_by(AuctionLot.updated_at.desc())
        .limit(limit_per_section)
    )
    for lot, auction in (await session.execute(ss_stmt)).all():
        bundle.showstoppers.append(
            {"lot": lot, "auction": auction, "reason": "showstopper"},
        )

    return bundle


async def closing_buckets(
    session: AsyncSession, *, now: datetime,
) -> ClosingBuckets:
    """Open biddable lots binned by effective_end time.

    effective_end = coalesce(lot.scheduled_end_at, auction.scheduled_end_at).
    Lots with NO end time at all are skipped (can't bin without a time).

    "End of today" / "end of tomorrow" are computed in America/Edmonton —
    a 23:30 UTC lot still belongs to "today" for a user in MT, even though
    it crosses midnight UTC.
    """
    now_local = now.astimezone(_LOCAL_TZ)
    end_of_today_local = now_local.replace(
        hour=23, minute=59, second=59, microsecond=999_999,
    )
    end_of_tomorrow_local = end_of_today_local + timedelta(days=1)
    end_of_today = end_of_today_local.astimezone(UTC)
    end_of_tomorrow = end_of_tomorrow_local.astimezone(UTC)

    effective_end = func.coalesce(
        AuctionLot.scheduled_end_at, Auction.scheduled_end_at,
    )

    stmt = (
        select(AuctionLot, Auction, effective_end.label("eff_end"))
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.lot_status.in_(OPEN_STATUSES),
            AuctionLot.user_action.is_distinct_from(UserAction.NOT_INTERESTED.value),
            effective_end.is_not(None),
            effective_end > now - _NOW_WINDOW,  # exclude very recently closed
            effective_end <= end_of_tomorrow,
        )
        .order_by(effective_end.asc())
    )
    rows = (await session.execute(stmt)).all()

    buckets = ClosingBuckets()
    now_cutoff = now + _NOW_WINDOW
    next_cutoff = now + _NEXT_WINDOW
    for lot, auction, eff_end in rows:
        item = {"lot": lot, "auction": auction, "effective_end": eff_end}
        if eff_end <= now_cutoff:
            buckets.now.append(item)
        elif eff_end <= next_cutoff:
            buckets.next_2h.append(item)
        elif eff_end <= end_of_today:
            buckets.today.append(item)
        else:
            buckets.tomorrow.append(item)
    return buckets


async def best_deals(
    session: AsyncSession, *, limit: int = 6, min_score: float = 0.10,
) -> list[dict[str, Any]]:
    """Top-N open lots by price_deal_score.

    Surfaces lots that *aren't* closing soon but are unusually cheap —
    the "you might want to look at this even though it's not urgent"
    section. min_score filters out anything weakly cheap; the homepage
    shouldn't lead with marginal finds.
    """
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.lot_status.in_(OPEN_STATUSES),
            AuctionLot.user_action.is_distinct_from(UserAction.NOT_INTERESTED.value),
            AuctionLot.price_deal_score.is_not(None),
            AuctionLot.price_deal_score >= min_score,
        )
        .order_by(AuctionLot.price_deal_score.desc(), AuctionLot.id.desc())
        .limit(limit)
    )
    return [
        {"lot": lot, "auction": auction}
        for lot, auction in (await session.execute(stmt)).all()
    ]


async def dashboard_kpis(
    session: AsyncSession, *, now: datetime, alerts_total: int,
) -> TodayKPIs:
    """The four numeric tiles in the above-the-fold KPI strip.

    `alerts_total` is passed in (computed by alerts_since in the route)
    so we don't re-run those three queries just to count rows. The other
    three values come from one COUNT-and-MAX query each.
    """
    effective_end = func.coalesce(
        AuctionLot.scheduled_end_at, Auction.scheduled_end_at,
    )
    now_cutoff = now + _NOW_WINDOW

    closing_now_stmt = (
        select(func.count())
        .select_from(AuctionLot)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.lot_status.in_(OPEN_STATUSES),
            effective_end.is_not(None),
            effective_end > now,
            effective_end <= now_cutoff,
        )
    )
    closing_now = (await session.execute(closing_now_stmt)).scalar_one()

    watching_stmt = (
        select(func.count())
        .select_from(AuctionLot)
        .where(AuctionLot.user_action == UserAction.INTERESTED.value)
    )
    watching = (await session.execute(watching_stmt)).scalar_one()

    best_stmt = (
        select(func.max(AuctionLot.price_deal_score))
        .where(AuctionLot.lot_status.in_(OPEN_STATUSES))
    )
    best_score = (await session.execute(best_stmt)).scalar_one_or_none()
    best_deal_pct = float(best_score) * 100 if best_score is not None else None

    return TodayKPIs(
        closing_now=closing_now,
        watching=watching,
        alerts=alerts_total,
        best_deal_pct=best_deal_pct,
    )
