"""Source-agnostic saved-search matching.

`match_listing` is a pure predicate over a `MatchableListing` (a flattened,
DB-free view of a candidate) and a `SavedSearch` (the ORM filter row). All
non-null filters AND together; NULL/empty filters are wildcards. String scalars
compare case-insensitively; `trim` is a substring match; list filters are
ANY-OF; year is a closed range; mileage and cost are inclusive caps. A NULL
listing field never satisfies a set filter.

`adapt_auction_lot` builds a `MatchableListing` from an auction lot + its
auction. A future private-sale source adds its own adapter here — the matcher
and every downstream read path stay unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

from carbuyer.db.models import Auction, AuctionLot, SavedSearch


@dataclass(frozen=True, slots=True)
class MatchableListing:
    source_kind: str
    source_id: int
    make: str | None
    model: str | None
    year: int | None
    trim: str | None
    mileage_km: int | None
    title_status: str | None
    condition_categorical: str | None
    province: str | None
    all_in_cost_cad: int | None
    rarity_score: float | None


def adapt_auction_lot(lot: AuctionLot, auction: Auction) -> MatchableListing:
    all_in = lot.all_in_at_current_bid_cad
    return MatchableListing(
        source_kind="auction_lot",
        source_id=lot.id,
        make=lot.make,
        model=lot.model,
        year=lot.year,
        trim=lot.trim,
        mileage_km=lot.mileage_km,
        title_status=lot.title_status,
        condition_categorical=lot.condition_categorical,
        province=auction.pickup_province,
        all_in_cost_cad=int(all_in) if all_in is not None else None,
        rarity_score=lot.rarity_score,
    )


def _eq_ci(value: str | None, want: str | None) -> bool:
    if want is None:
        return True
    return value is not None and value.casefold() == want.casefold()


def _contains_ci(value: str | None, want: str | None) -> bool:
    if want is None:
        return True
    return value is not None and want.casefold() in value.casefold()


def _any_of_ci(value: str | None, options: list[str] | None) -> bool:
    if not options:  # None or empty list → wildcard
        return True
    return value is not None and any(value.casefold() == o.casefold() for o in options)


def match_listing(listing: MatchableListing, search: SavedSearch) -> bool:
    # All non-null filters AND together; a NULL listing field never satisfies a set filter.
    year_min_ok = search.year_min is None or (
        listing.year is not None and listing.year >= search.year_min
    )
    year_max_ok = search.year_max is None or (
        listing.year is not None and listing.year <= search.year_max
    )
    mileage_ok = search.mileage_km_max is None or (
        listing.mileage_km is not None and listing.mileage_km <= search.mileage_km_max
    )
    cost_ok = search.max_all_in_cost_cad is None or (
        listing.all_in_cost_cad is not None
        and listing.all_in_cost_cad <= search.max_all_in_cost_cad
    )
    return (
        _eq_ci(listing.make, search.make)
        and _eq_ci(listing.model, search.model)
        and _contains_ci(listing.trim, search.trim)
        and year_min_ok
        and year_max_ok
        and mileage_ok
        and _any_of_ci(listing.title_status, search.title_status)
        and _any_of_ci(listing.condition_categorical, search.condition_categorical)
        and _any_of_ci(listing.province, search.province)
        and cost_ok
    )
