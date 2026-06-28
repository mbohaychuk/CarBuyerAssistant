"""Comp-set construction.

Pulls comparable sales from two sources:

- ``historical_sales``: cleansed, distilled records (any platform, any
  channel). Each row carries its own ``sale_channel`` so the fair-value
  computation can normalize to private-party.
- ``auction_lots``: lots already closed and sold within the recency window.
  These are MVP-source HiBid estate lots; we treat their ``final_bid_cad`` as
  an ``auction_estate`` channel observation. The auction-distiller (Phase 9)
  later promotes these into ``historical_sales`` with full BP/tax math.

The query stays in two SELECTs (no UNION) because:
- the two tables have different price columns (final_listed_price_cad vs.
  final_bid_cad) and different recency semantics, and
- statement_timeout=30s on the worker pool gives us no headroom to write
  one-clever-query that the planner gets wrong; two narrow indexed selects
  are predictable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import ListingStatus, LotStatus
from carbuyer.db.models import AuctionLot, HistoricalSale, PrivateListing

# A private listing is only a comp once it has DISAPPEARED — a sold/removed
# listing's last-seen asking price is a noisy sold-price proxy. An ACTIVE
# listing is an asking price, not a sale, so it never enters the comp set
# (using asking prices as comps would bias every fair value high).
_PRIVATE_COMP_STATUSES: tuple[str, ...] = (
    ListingStatus.SOLD.value,
    ListingStatus.REMOVED.value,
)

# Lot statuses representing a completed auction with a trustworthy
# `final_bid_cad`. CLOSED is the natural close path; FORCE_CLOSED is set by
# bid_poller after 24h past scheduled_end with the source still returning OPEN
# (the same branch writes final_bid_cad from current_high_bid_cad). Filtering
# CLOSED alone silently drops force-closed lots from the comp set — material
# in the sparse Western-Canada market the widened mileage band was meant to
# address.
_COMP_ELIGIBLE_STATUSES: tuple[str, ...] = (
    LotStatus.CLOSED.value,
    LotStatus.FORCE_CLOSED.value,
)

# Recency window for auction-lot comps. Older lots are out-of-distribution
# (market moves, seasonality) and the auction-distiller is expected to have
# promoted them to historical_sales by then anyway.
#
# Load-bearing with carbuyer.apps.auction_distiller.distiller.DISTILL_AGE_DAYS:
# they MUST stay equal. Distiller deletes lots older than DISTILL_AGE_DAYS from
# auction_lots after copying them into historical_sales. If this window is
# shorter than DISTILL, lots vanish before the comp builder is done with them
# (gap). If longer, the same sale appears in both tables (double-counted).
RECENT_AUCTION_LOTS_DAYS = 14


@dataclass(frozen=True, slots=True)
class ComparableSale:
    price_cad: Decimal
    sale_channel: str
    year: int | None
    mileage_km: int | None
    days_listed: int | None
    disposition_reason: str
    source: str  # "historical_sales" | "auction_lots" | "private_listing"


async def build_comp_set(
    session: AsyncSession,
    *,
    make: str,
    model: str,
    trim: str | None,
    year: int,
    mileage_km: int,
    year_window: int = 1,
    mileage_pct: float = 0.30,
) -> list[ComparableSale]:
    """Return historical + recent-auction comps within make/model/year/mileage band.

    The trim filter is generous: when ``trim`` is supplied, rows with the
    matching trim OR with NULL trim are accepted. Rows with a different
    explicit trim are excluded. Untrimmed comps are common for base-spec
    listings and excluding them here would shrink Western-Canada comp sets to
    near-zero on niche models.
    """
    mileage_lo = int(mileage_km * (1 - mileage_pct))
    mileage_hi = int(mileage_km * (1 + mileage_pct))

    # Case-insensitive matching on make/model/trim: the enricher captures
    # vendor casing as-is (HiBid emits "Ford" / "FORD" / "ford" in different
    # records; LLM normalization is best-effort). A strict ``==`` here means
    # one mis-cased comp row is invisible to a candidate, silently shrinking
    # the comp set and forcing INSUFFICIENT verdicts on lots that actually
    # have data. UPPER() on both sides aligns with how the value-pending-lots
    # tool's ``requeue`` already matches.
    make_u = make.upper()
    model_u = model.upper()
    trim_u = trim.upper() if trim else None

    hs_stmt = select(HistoricalSale).where(
        func.upper(HistoricalSale.make) == make_u,
        func.upper(HistoricalSale.model) == model_u,
        HistoricalSale.year.between(year - year_window, year + year_window),
        HistoricalSale.mileage_km.between(mileage_lo, mileage_hi),
    )
    if trim_u:
        hs_stmt = hs_stmt.where(
            or_(
                func.upper(HistoricalSale.trim) == trim_u,
                HistoricalSale.trim.is_(None),
            ),
        )
    hs_rows = (await session.execute(hs_stmt)).scalars().all()

    cutoff = datetime.now(UTC) - timedelta(days=RECENT_AUCTION_LOTS_DAYS)
    al_stmt = select(AuctionLot).where(
        func.upper(AuctionLot.make) == make_u,
        func.upper(AuctionLot.model) == model_u,
        AuctionLot.year.between(year - year_window, year + year_window),
        AuctionLot.mileage_km.between(mileage_lo, mileage_hi),
        AuctionLot.lot_status.in_(_COMP_ELIGIBLE_STATUSES),
        AuctionLot.closed_at >= cutoff,
        AuctionLot.final_bid_cad.is_not(None),
    )
    if trim_u:
        al_stmt = al_stmt.where(
            or_(
                func.upper(AuctionLot.trim) == trim_u,
                AuctionLot.trim.is_(None),
            ),
        )
    al_rows = (await session.execute(al_stmt)).scalars().all()

    # Disappeared private listings (sold/removed) as private-channel comps —
    # make/model/year/mileage live on the vehicle_offer parent (inherited),
    # listing_status + asking_price on the private_listing child.
    pl_stmt = select(PrivateListing).where(
        func.upper(PrivateListing.make) == make_u,
        func.upper(PrivateListing.model) == model_u,
        PrivateListing.year.between(year - year_window, year + year_window),
        PrivateListing.mileage_km.between(mileage_lo, mileage_hi),
        PrivateListing.listing_status.in_(_PRIVATE_COMP_STATUSES),
        PrivateListing.asking_price_cad.is_not(None),
    )
    if trim_u:
        pl_stmt = pl_stmt.where(
            or_(
                func.upper(PrivateListing.trim) == trim_u,
                PrivateListing.trim.is_(None),
            ),
        )
    pl_rows = (await session.execute(pl_stmt)).scalars().all()

    comps: list[ComparableSale] = []
    for h in hs_rows:
        # Prefer the all-in price (with BP) when present; fall back to the
        # listed price for rows the distiller didn't get a final bid for.
        price = h.final_price_with_premium_cad or h.final_listed_price_cad
        if price is None:
            continue
        comps.append(
            ComparableSale(
                price_cad=Decimal(price),
                sale_channel=h.sale_channel,
                year=h.year,
                mileage_km=h.mileage_km,
                days_listed=h.days_listed,
                disposition_reason=h.disposition_reason,
                source="historical_sales",
            )
        )
    for lot in al_rows:
        bid = lot.final_bid_cad
        if bid is None:
            continue
        # MVP: every AuctionLot comes from an estate-class auction source
        # (HiBid). When McDougall / RB land in Phase 10, this will need to
        # read from the joined Auction.auction_subtype to choose the channel.
        comps.append(
            ComparableSale(
                price_cad=Decimal(bid),
                sale_channel="auction_estate",
                year=lot.year,
                mileage_km=lot.mileage_km,
                days_listed=None,
                disposition_reason="sold",
                source="auction_lots",
            )
        )
    for pl in pl_rows:
        price = pl.asking_price_cad
        if price is None:
            continue
        # Last-seen asking as the sold proxy; the private channel multiplier is
        # 1.00, so no further adjustment here.
        comps.append(
            ComparableSale(
                price_cad=Decimal(price),
                sale_channel="private",
                year=pl.year,
                mileage_km=pl.mileage_km,
                days_listed=pl.days_on_market,
                disposition_reason=pl.listing_status,
                source="private_listing",
            )
        )
    return comps
