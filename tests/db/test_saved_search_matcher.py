from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from carbuyer.db.models import Auction, AuctionLot, SavedSearch
from carbuyer.db.saved_searches import MatchableListing, adapt_auction_lot, match_listing


def _listing(**overrides: object) -> MatchableListing:
    base: dict[str, object] = dict(
        source_kind="auction_lot", source_id=1,
        make="Ford", model="Mustang", year=1968, trim="Fastback GT",
        mileage_km=90_000, title_status="NORMAL",
        condition_categorical="good", province="AB",
        all_in_cost_cad=25_000, rarity_score=2.1,
    )
    base.update(overrides)
    return MatchableListing(**base)  # type: ignore[arg-type]


def test_empty_search_matches_everything() -> None:
    assert match_listing(_listing(), SavedSearch(name="all")) is True


def test_make_case_insensitive_eq() -> None:
    assert match_listing(_listing(make="ford"), SavedSearch(name="x", make="FORD")) is True
    assert match_listing(_listing(make="Toyota"), SavedSearch(name="x", make="Ford")) is False


def test_make_null_listing_fails_when_filter_set() -> None:
    assert match_listing(_listing(make=None), SavedSearch(name="x", make="Ford")) is False


def test_model_case_insensitive_eq() -> None:
    assert match_listing(_listing(model="MUSTANG"), SavedSearch(name="x", model="mustang")) is True
    assert match_listing(_listing(model="Camaro"), SavedSearch(name="x", model="Mustang")) is False
    assert match_listing(_listing(model=None), SavedSearch(name="x", model="Mustang")) is False


def test_trim_substring_case_insensitive() -> None:
    # trim is a contains-match, not equality.
    assert match_listing(_listing(trim="Fastback GT"), SavedSearch(name="x", trim="gt")) is True
    assert match_listing(_listing(trim="Coupe"), SavedSearch(name="x", trim="gt")) is False
    assert match_listing(_listing(trim=None), SavedSearch(name="x", trim="gt")) is False


def test_year_range_inclusive_both_bounds() -> None:
    s = SavedSearch(name="x", year_min=1965, year_max=1970)
    assert match_listing(_listing(year=1965), s) is True
    assert match_listing(_listing(year=1970), s) is True
    assert match_listing(_listing(year=1964), s) is False
    assert match_listing(_listing(year=1971), s) is False


def test_year_open_bounds() -> None:
    assert match_listing(_listing(year=1990), SavedSearch(name="x", year_min=1980)) is True
    assert match_listing(_listing(year=1975), SavedSearch(name="x", year_min=1980)) is False
    assert match_listing(_listing(year=1975), SavedSearch(name="x", year_max=1980)) is True
    assert match_listing(_listing(year=None), SavedSearch(name="x", year_min=1980)) is False


def test_mileage_max_inclusive() -> None:
    s = SavedSearch(name="x", mileage_km_max=100_000)
    assert match_listing(_listing(mileage_km=100_000), s) is True
    assert match_listing(_listing(mileage_km=100_001), s) is False
    assert match_listing(_listing(mileage_km=None), s) is False


def test_title_status_any_of_case_insensitive() -> None:
    s = SavedSearch(name="x", title_status=["NORMAL", "REBUILT"])
    assert match_listing(_listing(title_status="normal"), s) is True
    assert match_listing(_listing(title_status="SALVAGE"), s) is False
    assert match_listing(_listing(title_status=None), s) is False


def test_condition_any_of() -> None:
    s = SavedSearch(name="x", condition_categorical=["good", "excellent"])
    assert match_listing(_listing(condition_categorical="good"), s) is True
    assert match_listing(_listing(condition_categorical="rough"), s) is False
    assert match_listing(_listing(condition_categorical=None), s) is False


def test_province_any_of_case_insensitive() -> None:
    s = SavedSearch(name="x", province=["AB", "SK"])
    assert match_listing(_listing(province="ab"), s) is True
    assert match_listing(_listing(province="MB"), s) is False
    assert match_listing(_listing(province=None), s) is False


def test_max_all_in_cost_inclusive() -> None:
    s = SavedSearch(name="x", max_all_in_cost_cad=30_000)
    assert match_listing(_listing(all_in_cost_cad=30_000), s) is True
    assert match_listing(_listing(all_in_cost_cad=30_001), s) is False
    assert match_listing(_listing(all_in_cost_cad=None), s) is False


def test_empty_list_filter_is_treated_as_wildcard() -> None:
    # A persisted empty array (no options chosen) must not exclude everything.
    assert match_listing(_listing(province="AB"), SavedSearch(name="x", province=[])) is True


def test_all_filters_and_together() -> None:
    s = SavedSearch(
        name="dream", make="Ford", model="Mustang",
        year_min=1965, year_max=1970, mileage_km_max=120_000,
        title_status=["NORMAL"], province=["AB"], max_all_in_cost_cad=40_000,
    )
    assert match_listing(_listing(), s) is True
    # one field off → no match
    assert match_listing(_listing(province="BC"), s) is False


def test_adapt_rounds_all_in_cost_up() -> None:
    auction = Auction(
        source="t", source_auction_id="A", url="u", canonical_url="u",
        auction_subtype="estate", first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC), pickup_province="AB",
    )
    lot = AuctionLot(
        auction=auction, source_lot_id="L1", url="u1", title="car",
        make="Ford", all_in_at_current_bid_cad=Decimal("30000.01"),
    )
    listing = adapt_auction_lot(lot, auction)
    expected_ceil = 30_001  # ceil of 30000.01 — not the truncated 30_000
    assert listing.all_in_cost_cad == expected_ceil
    assert listing.province == "AB"
