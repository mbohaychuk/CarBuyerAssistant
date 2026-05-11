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

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import AuctionLot, HistoricalSale

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
    source: str  # "historical_sales" | "auction_lots"


async def build_comp_set(
    session: AsyncSession,
    *,
    make: str,
    model: str,
    trim: str | None,
    year: int,
    mileage_km: int,
    year_window: int = 1,
    mileage_pct: float = 0.20,
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

    hs_stmt = select(HistoricalSale).where(
        HistoricalSale.make == make,
        HistoricalSale.model == model,
        HistoricalSale.year.between(year - year_window, year + year_window),
        HistoricalSale.mileage_km.between(mileage_lo, mileage_hi),
    )
    if trim:
        hs_stmt = hs_stmt.where(
            or_(HistoricalSale.trim == trim, HistoricalSale.trim.is_(None)),
        )
    hs_rows = (await session.execute(hs_stmt)).scalars().all()

    cutoff = datetime.now(UTC) - timedelta(days=RECENT_AUCTION_LOTS_DAYS)
    al_stmt = select(AuctionLot).where(
        AuctionLot.make == make,
        AuctionLot.model == model,
        AuctionLot.year.between(year - year_window, year + year_window),
        AuctionLot.mileage_km.between(mileage_lo, mileage_hi),
        AuctionLot.lot_status == "closed",
        AuctionLot.closed_at >= cutoff,
        AuctionLot.final_bid_cad.is_not(None),
    )
    if trim:
        al_stmt = al_stmt.where(
            or_(AuctionLot.trim == trim, AuctionLot.trim.is_(None)),
        )
    al_rows = (await session.execute(al_stmt)).scalars().all()

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
    return comps
