"""Tests for the Kijiji __NEXT_DATA__ parser.

Fixtures are real kijiji.ca captures (2026-05-30) — see fixtures/README.md.
Re-capture and re-inspect the Apollo JSON if Kijiji re-platforms off Next.js.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from carbuyer.sources.kijiji.parser import (
    KijijiListing,
    parse_listing_detail,
    parse_search_page,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Real listing ids present in the captured fixtures.
_JEEP_ID = "1738329373"   # 2016 Jeep Cherokee Trailhawk — happy path
_SPARSE_ID = "1736878962"  # "Hon" — degenerate listing ($99,999,999, ~empty desc)

# Expected counts / values derived from the captured fixtures.
_OWNER_PAGE_TOTAL = 45
_MIXED_PAGE_TOTAL = 46
_MIXED_DEALERS = 39
_MIXED_OWNERS = 7
_CONTACT_PRICE_COUNT = 2          # "Please Contact" rows on the owner page
_JEEP_YEAR = 2016
_JEEP_MILEAGE_KM = 115000
_SEARCH_PHOTO_COUNT = 5           # search page carries the first few photos
_DETAIL_PHOTO_COUNT = 15          # detail page carries the full set
_MIN_FULL_DESC_LEN = 200          # detail description is longer than the stub


def _read(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


def _by_id(entries: list[KijijiListing], listing_id: str) -> KijijiListing:
    return next(e for e in entries if e.listing_id == listing_id)


# ── search page ──────────────────────────────────────────────────────────────


def test_owner_search_page_yields_all_owner_listings() -> None:
    entries = parse_search_page(_read("search_owner_alberta.html"))
    assert len(entries) == _OWNER_PAGE_TOTAL
    # The owner-filtered URL returns owners only.
    assert all(not e.is_dealer for e in entries)
    # Every entry carries the upsert key.
    assert all(e.listing_id and e.url for e in entries)


def test_mixed_search_page_flags_dealers_vs_owners() -> None:
    entries = parse_search_page(_read("search_mixed_alberta.html"))
    assert len(entries) == _MIXED_PAGE_TOTAL
    dealers = [e for e in entries if e.is_dealer]
    owners = [e for e in entries if not e.is_dealer]
    assert len(dealers) == _MIXED_DEALERS
    assert len(owners) == _MIXED_OWNERS


def test_search_entry_field_mapping() -> None:
    entry = _by_id(parse_search_page(_read("search_owner_alberta.html")), _JEEP_ID)
    assert entry.title == "2016 Jeep Cherokee Trailhawk  115 KM"
    # price.amount is in cents: 1399900 -> $13,999.00.
    assert entry.ask_price_cad == Decimal("13999")
    # Normalized structured attributes are authoritative on the search page.
    assert entry.year == _JEEP_YEAR
    assert entry.make == "jeep"
    assert entry.model == "cherokee"
    assert entry.trim == "Trailhawk"
    assert entry.mileage_km == _JEEP_MILEAGE_KM
    assert entry.city == "Edmonton"
    assert entry.province == "AB"
    # Search page carries the first few photos only.
    assert len(entry.photos) == _SEARCH_PHOTO_COUNT
    assert entry.description is not None


def test_contact_price_listings_have_no_ask_price() -> None:
    entries = parse_search_page(_read("search_owner_alberta.html"))
    # Two listings on this page are "Please Contact" (NonAmountPrice).
    assert sum(1 for e in entries if e.ask_price_cad is None) == _CONTACT_PRICE_COUNT


# ── detail page ────────────────────────────────────────────────────────────────


def test_detail_page_happy_path() -> None:
    entry = parse_listing_detail(_read("listing_detail_jeep_cherokee.html"))
    assert entry is not None
    assert entry.listing_id == _JEEP_ID
    # caryear is empty on the detail page -> year falls back to the title.
    assert entry.year == _JEEP_YEAR
    assert entry.make == "jeep"
    assert entry.model == "cherokee"
    assert entry.ask_price_cad == Decimal("13999")
    assert entry.city == "Edmonton"
    assert entry.province == "AB"
    # Detail page carries the full photo set + full description.
    assert len(entry.photos) == _DETAIL_PHOTO_COUNT
    assert entry.description is not None
    assert len(entry.description) > _MIN_FULL_DESC_LEN


def test_detail_page_degenerate_listing() -> None:
    entry = parse_listing_detail(_read("listing_detail_sparse.html"))
    assert entry is not None
    assert entry.listing_id == _SPARSE_ID
    assert entry.title == "Hon"
    # No year derivable from "Hon" and caryear is empty -> None (the valuator
    # then marks it insufficient, so it never alerts).
    assert entry.year is None
    assert entry.make == "honda"
    assert entry.model == "accord"
    # A garbage seller price flows through faithfully.
    assert entry.ask_price_cad == Decimal("99999999")
    assert entry.province == "AB"


# ── resilience ──────────────────────────────────────────────────────────────


def test_parsers_degrade_on_unparseable_html() -> None:
    bad_json_page = (
        '<script id="__NEXT_DATA__" type="application/json">{bad json</script>'
    )
    assert parse_search_page("") == []
    assert parse_search_page("<html><body>nope</body></html>") == []
    assert parse_search_page(bad_json_page) == []
    assert parse_listing_detail("") is None
    assert parse_listing_detail("<html></html>") is None
