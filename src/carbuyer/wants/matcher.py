"""Does a vehicle lot satisfy a want? The single source of truth for matching.

Pure function over an AuctionLot (Phase 0) — no DB, no session — so it is fast to
unit-test and trivial to call per-lot from the valuator's post-valuation hook. The
bulk "all lots matching this want" SQL query (dashboard) is a separate, later seam
that must agree with this predicate.

Channel-specific values (price, pickup province) are passed in as keywords rather
than read off the lot, so the predicate touches only source-agnostic vehicle facts.
When auction_lots splits into a vehicle_offer parent + auction_lot/private_listing
children (Phase 1), the price source differs per channel (current high bid vs asking
price) — keeping it injected makes that a caller change, not a matcher rewrite.

Policy: LENIENT on unknown attributes. A buyer-assistant should rather raise an
extra dismissable alert than silently miss a deal, so a missing trim / transmission
/ mileage / price does NOT exclude a lot. Only the core identity (make, model,
year) requires a known, matching value; an explicit province filter likewise needs
a known location.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import get_args

from carbuyer.db.models import VehicleOffer
from carbuyer.llm.schemas import Condition
from carbuyer.wants.criteria import WantCriteria

# Worst → best; index gives an orderable rank for the condition floor. Guarded
# against drift from the Condition vocabulary it mirrors (set-equality, so the
# ranking order stays ours to choose).
_CONDITION_ORDER = ("bad", "poor", "decent", "good", "great")
assert set(_CONDITION_ORDER) == set(get_args(Condition)), (
    "_CONDITION_ORDER drifted from carbuyer.llm.schemas.Condition"
)


def matches(
    lot: VehicleOffer,
    criteria: WantCriteria,
    *,
    pickup_province: str | None = None,
    offer_price_cad: int | Decimal | None = None,
) -> bool:
    checks = (
        not (criteria.hide_showstoppers and lot.showstopper_flags),
        _in_set(lot.make, criteria.makes, lenient_unknown=False),
        _in_set(lot.model, criteria.models, lenient_unknown=False),
        _in_set(lot.trim, criteria.trims, lenient_unknown=True),
        _in_set(lot.transmission, criteria.transmissions, lenient_unknown=True),
        _in_set(lot.drivetrain, criteria.drivetrains, lenient_unknown=True),
        _year_in_range(lot.year, criteria.year_min, criteria.year_max),
        _at_most(offer_price_cad, criteria.price_ceiling_cad),
        _at_most(lot.mileage_km, criteria.max_mileage_km),
        _province_ok(pickup_province, criteria.provinces),
        _condition_ok(
            lot.condition_categorical,
            criteria.condition_min,
            sparse=bool(lot.condition_inferred_from_sparse_listing),
        ),
    )
    return all(checks)


def could_match_any_want(
    *,
    make: str | None,
    model: str | None,
    year: int | None,
    title: str | None,
    criteria_list: Sequence[WantCriteria],
) -> bool:
    """Cheap upstream gate (WG1): does this RAW offer plausibly match at least one
    want, using only scraped fields — no LLM? Keeps cost off the firehose: a lot
    that matches no want is never enriched/valued/stored.

    Make/model are checked against the parsed fields OR the raw title text, so a lot
    whose make lives only in the title isn't dropped. Year only excludes when known
    and out of range. Everything the LLM fills (trim/transmission/condition) is
    ignored here — the precise `matches()` runs post-enrichment to create the actual
    want_match. Empty `criteria_list` (no active wants) matches nothing.
    """
    # ponytail: substring title scan, not word-boundary — a coarse gate's false
    # positives cost one enrichment; tighten to \b boundaries only if they show up.
    hay = f"{make or ''} {model or ''} {title or ''}".lower()
    return any(_coarse_match(c, year, hay) for c in criteria_list)


def _coarse_match(c: WantCriteria, year: int | None, hay: str) -> bool:
    if c.makes and not _any_term_in(c.makes, hay):
        return False
    if c.models and not _any_term_in(c.models, hay):
        return False
    if year is not None:  # unknown year is lenient (kept); known year must fit
        if c.year_min is not None and year < c.year_min:
            return False
        if c.year_max is not None and year > c.year_max:
            return False
    return True


def _any_term_in(terms: Sequence[str], hay: str) -> bool:
    return any(t.strip().lower() in hay for t in terms if t.strip())


def _in_set(value: str | None, allowed: Sequence[str], *, lenient_unknown: bool) -> bool:
    """Case-insensitive membership. Empty `allowed` = "any". A missing value —
    None, blank, or the "unknown" sentinel — is lenient (kept) or strict
    (dropped) per `lenient_unknown`.
    """
    if not allowed:
        return True
    needle = "" if value is None else value.strip().lower()
    if needle in ("", "unknown"):
        return lenient_unknown
    return any(needle == a.strip().lower() for a in allowed)


def _year_in_range(year: int | None, ymin: int | None, ymax: int | None) -> bool:
    if ymin is None and ymax is None:
        return True
    if year is None:
        return False
    return (ymin is None or year >= ymin) and (ymax is None or year <= ymax)


def _at_most(value: int | Decimal | None, ceiling: int | None) -> bool:
    """True unless both are known and value exceeds the ceiling (lenient on None)."""
    return ceiling is None or value is None or value <= ceiling


def _province_ok(pickup_province: str | None, provinces: Sequence[str]) -> bool:
    if not provinces:
        return True
    if pickup_province is None or not pickup_province.strip():
        return False
    return pickup_province.strip().upper() in {p.strip().upper() for p in provinces}


def _condition_ok(condition: str | None, floor: str | None, *, sparse: bool) -> bool:
    # No floor, an unknown/unrecognized rating, or a sparse-listing-inferred
    # rating (a coerced "decent" that is really unknown) → lenient.
    if floor is None or sparse or condition not in _CONDITION_ORDER:
        return True
    return _CONDITION_ORDER.index(condition) >= _CONDITION_ORDER.index(floor)
