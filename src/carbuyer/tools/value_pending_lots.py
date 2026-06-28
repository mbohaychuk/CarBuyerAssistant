"""CLI helper for seeding pending-lot valuations.

Three subcommands, each safe to run repeatedly:

  list-groups       Emit JSON describing (year, make, model) groups whose lots
                    have valuation_status in (PENDING, INSUFFICIENT). Includes
                    the candidate mileage range and trim variety so an operator
                    can scope its research.

  write-comps       Read a JSON array of comp dicts from stdin and INSERT them
                    into historical_sales tagged ``sale_platform='llm_estimate'``.
                    Existing llm_estimate rows for the same (year, make, model)
                    are deleted first so re-runs replace rather than accumulate.

  requeue           For one (year, make, model), set valuation_status=PENDING
                    on matching open lots and NOTIFY valuation_pending so the
                    existing carbuyer-valuator.service picks them up.

An operator orchestrates these
directly. Business logic (what to research, when to give up) lives in the
operator's research; data plumbing lives here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.sql import text

from carbuyer.db.enums import ValuationStatus
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale
from carbuyer.db.notify import notify
from carbuyer.db.session import get_session

# Tag used on every synthetic row. Lets us delete-and-replace cleanly without
# touching real auction-derived comps. Read by carbuyer-distiller too — it
# leaves llm_estimate rows alone (it only promotes real auction outcomes).
SYNTHETIC_SALE_PLATFORM = "llm_estimate"

# Common Canadian-market makes for the hybrid strict/loose comp threshold.
# These get strict (5+ comps); everything else gets loose (3+).
COMMON_MAKES: frozenset[str] = frozenset({
    "TOYOTA", "FORD", "CHEVROLET", "CHEV", "GMC", "DODGE", "RAM",
    "HONDA", "NISSAN", "HYUNDAI", "KIA", "MAZDA", "SUBARU",
    "VOLKSWAGEN", "VW", "JEEP", "CHRYSLER", "ACURA", "BUICK",
    "LINCOLN", "CADILLAC", "MITSUBISHI", "MINI",
})


async def cmd_list_groups(args: argparse.Namespace) -> None:
    """Emit JSON describing groups in need of comp research."""
    # Filter by auction.scheduled_end_at (the real source of truth for "is
    # this lot still actionable") rather than auction_lots.lot_status, which
    # the bid_poller has been observed mismarking as 'closed' for upcoming
    # lots that haven't opened bidding yet. The skill needs to work on those
    # too — they'll be biddable when the auction starts.
    now = datetime.now(UTC)
    async with get_session() as s:
        stmt = (
            select(
                AuctionLot.year,
                AuctionLot.make,
                AuctionLot.model,
                func.count(AuctionLot.id).label("candidate_count"),
                func.min(AuctionLot.mileage_km).label("min_km"),
                func.max(AuctionLot.mileage_km).label("max_km"),
                func.array_agg(func.distinct(AuctionLot.trim)).label("trims"),
            )
            .join(Auction, Auction.id == AuctionLot.auction_id)
            .where(
                AuctionLot.valuation_status.in_(
                    (
                        ValuationStatus.PENDING,
                        ValuationStatus.INSUFFICIENT,
                        ValuationStatus.SKIPPED,
                    ),
                ),
                AuctionLot.year.is_not(None),
                AuctionLot.make.is_not(None),
                AuctionLot.model.is_not(None),
                Auction.scheduled_end_at > now,
            )
            .group_by(AuctionLot.year, AuctionLot.make, AuctionLot.model)
            .order_by(func.count(AuctionLot.id).desc())
        )
        if args.limit:
            stmt = stmt.limit(args.limit)
        rows = (await s.execute(stmt)).all()

    groups = [
        {
            "year": r.year,
            "make": r.make,
            "model": r.model,
            "candidate_count": r.candidate_count,
            "min_km": r.min_km,
            "max_km": r.max_km,
            "trims": [t for t in (cast(list[str | None], r.trims) or []) if t is not None],
            "common_make": (r.make or "").upper() in COMMON_MAKES,
            "min_comps_required": 5 if (r.make or "").upper() in COMMON_MAKES else 3,
        }
        for r in rows
    ]
    json.dump(groups, sys.stdout, indent=2)
    sys.stdout.write("\n")


async def cmd_write_comps(args: argparse.Namespace) -> None:
    """Replace synthetic comps for affected (year, make, model) groups."""
    payload = sys.stdin.read().strip()
    if not payload:
        sys.exit("write-comps: stdin is empty")
    try:
        comps = json.loads(payload)
    except json.JSONDecodeError as exc:
        sys.exit(f"write-comps: invalid JSON on stdin: {exc}")
    if not isinstance(comps, list) or not comps:
        sys.exit("write-comps: expected non-empty JSON array on stdin")
    comps_list = cast(list[Any], comps)
    affected_groups: set[tuple[int, str, str]] = set()
    valid: list[dict[str, Any]] = []
    for i, raw in enumerate(comps_list):
        if not isinstance(raw, dict):
            sys.exit(f"write-comps: row {i} is not an object")
        c = cast(dict[str, Any], raw)
        try:
            year = int(c["year"])
            make = str(c["make"]).strip().upper()
            model = str(c["model"]).strip().upper()
            mileage_km = int(c["mileage_km"])
            price_cad = Decimal(str(c["price_cad"]))
            sale_channel = str(c["sale_channel"]).strip().lower()
        except (KeyError, TypeError, ValueError) as exc:
            sys.exit(f"write-comps: row {i} missing/invalid required field: {exc}")
        if sale_channel not in {"private", "dealer", "auction_estate"}:
            sys.exit(
                f"write-comps: row {i} has unsupported sale_channel "
                f"{sale_channel!r}; use 'private' | 'dealer' | 'auction_estate'",
            )
        valid.append({
            "year": year, "make": make, "model": model,
            "mileage_km": mileage_km, "price_cad": price_cad,
            "sale_channel": sale_channel,
            "trim": c.get("trim") or None,
            "condition_categorical": c.get("condition_categorical") or None,
            "source_url": c.get("source_url") or None,
            "notes": c.get("notes") or None,
        })
        affected_groups.add((year, make, model))

    async with get_session() as s, s.begin():
        # Delete-and-replace existing synthetic comps per affected group so
        # re-running the skill refreshes rather than accumulates duplicates.
        # Distiller leaves llm_estimate rows alone, so we only touch our own.
        for year, make, model in affected_groups:
            await s.execute(
                delete(HistoricalSale).where(
                    HistoricalSale.year == year,
                    HistoricalSale.make == make,
                    HistoricalSale.model == model,
                    HistoricalSale.sale_platform == SYNTHETIC_SALE_PLATFORM,
                ),
            )

        for c in valid:
            note_parts: list[str] = []
            if c["source_url"]:
                note_parts.append(f"src={c['source_url']}")
            if c["notes"]:
                note_parts.append(c["notes"])
            row = HistoricalSale(
                year=c["year"],
                make=c["make"],
                model=c["model"],
                trim=c["trim"],
                mileage_km=c["mileage_km"],
                condition_categorical=c["condition_categorical"],
                final_listed_price_cad=c["price_cad"],
                sale_channel=c["sale_channel"],
                sale_platform=SYNTHETIC_SALE_PLATFORM,
                title_status="UNKNOWN",
                disposition_reason="unknown",
                notes=" | ".join(note_parts) if note_parts else None,
                schema_version=1,
            )
            s.add(row)

    print(json.dumps({
        "rows_written": len(valid),
        "groups_refreshed": [
            {"year": y, "make": m, "model": mo}
            for y, m, mo in sorted(affected_groups)
        ],
    }, indent=2))


async def cmd_requeue(args: argparse.Namespace) -> None:
    """Set valuation_status=PENDING for matching open lots and NOTIFY."""
    year = args.year
    make = args.make.strip().upper()
    model = args.model.strip().upper()

    now = datetime.now(UTC)
    async with get_session() as s, s.begin():
        # Same scheduled_end_at filter as list-groups (bid_poller-induced
        # 'closed' lot_status is not reliable for "is the lot still useful").
        target_ids_stmt = (
            select(AuctionLot.id)
            .join(Auction, Auction.id == AuctionLot.auction_id)
            .where(
                AuctionLot.year == year,
                func.upper(AuctionLot.make) == make,
                func.upper(AuctionLot.model) == model,
                Auction.scheduled_end_at > now,
                AuctionLot.valuation_status.in_(
                    (
                        ValuationStatus.PENDING,
                        ValuationStatus.INSUFFICIENT,
                        ValuationStatus.SKIPPED,
                    ),
                ),
            )
        )
        target_ids = [r[0] for r in (await s.execute(target_ids_stmt)).all()]
        if target_ids:
            await s.execute(
                update(AuctionLot)
                .where(AuctionLot.id.in_(target_ids))
                .values(valuation_status=ValuationStatus.PENDING),
            )
        ids = target_ids

        # One NOTIFY per requeued lot. Payload is the lot id so the valuator
        # can pick it up directly without re-scanning. Cheaper than a single
        # bulk NOTIFY because the listener's catchup-sweep is otherwise the
        # only path to discover requeued rows.
        for lot_id in ids:
            await notify(s, "valuation_pending", str(lot_id))

    print(json.dumps({
        "year": year, "make": make, "model": model,
        "requeued_count": len(ids),
        "lot_ids": ids,
    }, indent=2))


async def cmd_summary(args: argparse.Namespace) -> None:
    """Show pipeline state — how many lots are at each valuation status."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT vo.valuation_status, count(*) "
            "FROM auction_lot l "
            "JOIN vehicle_offer vo ON vo.id = l.id "
            "JOIN auctions a ON a.id = l.auction_id "
            "WHERE a.scheduled_end_at > now() "
            "GROUP BY vo.valuation_status ORDER BY count(*) DESC",
        ))).all()
        synth = (await s.execute(text(
            "SELECT count(*) FROM historical_sales "
            "WHERE sale_platform = :p",
        ), {"p": SYNTHETIC_SALE_PLATFORM})).scalar_one()
    print(json.dumps({
        "active_lots_by_valuation_status": {r[0]: r[1] for r in rows},
        "synthetic_comp_count": synth,
    }, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(prog="value-pending-lots")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list-groups", help="emit JSON of unvalued groups")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_list_groups)

    sp = sub.add_parser("write-comps", help="batch-insert comps from stdin JSON")
    sp.set_defaults(func=cmd_write_comps)

    sp = sub.add_parser("requeue", help="re-pend lots for valuation")
    sp.add_argument("--year", type=int, required=True)
    sp.add_argument("--make", required=True)
    sp.add_argument("--model", required=True)
    sp.set_defaults(func=cmd_requeue)

    sp = sub.add_parser("summary", help="show pipeline state")
    sp.set_defaults(func=cmd_summary)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
