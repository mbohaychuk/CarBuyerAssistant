"""Tests for the post-SPA HiBid parser.

Fixtures are real captured responses (see fixtures/README would-be-nice).
Re-capture via tools/regen_hibid_fixtures or similar if HiBid re-platforms.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from carbuyer.sources.hibid.parser import (
    HibidAuctionDetails,
    HibidLotSummary,
    discover_auction_ids,
    lot_is_closed,
    parse_auction_details_response,
    parse_lot_record,
    parse_lot_search_response,
)

FIX = Path("tests/sources/hibid/fixtures")


# ── discovery ────────────────────────────────────────────────────────────


def test_discover_auction_ids_against_live_html_fixture() -> None:
    """The SSR'd province list page renders auction cards as plain HTML —
    discover_auction_ids must extract every auction id without false
    positives from category-nav links."""
    html = (FIX / "discover_alberta.html").read_text()
    ids = discover_auction_ids(html)
    # Real Alberta page had multiple auctions on capture day.
    assert len(ids) >= 1
    # Each id is a numeric string.
    assert all(i.isdigit() for i in ids)
    # No duplicates (regex was iterated with seen-set).
    assert len(ids) == len(set(ids))


def test_discover_auction_ids_synthetic_filters_category_links() -> None:
    """`/lots/700006/...` and `/category/...` links must NOT match — only
    the `/<province>/auction/<id>/<slug>` pattern."""
    html = """
    <html><body>
      <a href="/alberta/auction/123456/spring-equipment">Spring</a>
      <a href="/alberta/auction/789012/farm-dispersal">Farm</a>
      <a href="/lots/700006/cars-and-vehicles">Vehicles category</a>
      <a href="/alberta/lots?status=HOT">Hot lots</a>
      <a href="/alberta/auction/123456/spring-equipment">Dupe</a>
    </body></html>
    """
    assert discover_auction_ids(html) == ["123456", "789012"]


def test_discover_auction_ids_empty_when_no_matches() -> None:
    assert discover_auction_ids("<html>nothing</html>") == []


# ── lot search (GraphQL response) ────────────────────────────────────────


def test_parse_lot_search_response_against_live_fixture() -> None:
    """The captured response contains 79 real lots — parsing must yield
    79 HibidLotSummary entries with required fields populated."""
    data = json.loads((FIX / "lot_search.json").read_text())
    lots = parse_lot_search_response(data)
    assert len(lots) == 79
    # First lot should have a stable itemId and a populated lead.
    first = lots[0]
    assert first.source_lot_id  # non-empty
    assert first.source_lot_id.isdigit()
    assert first.title  # non-empty lead
    assert isinstance(first.photos, list)
    # extra must carry the lotState dict for downstream consumers.
    assert "lotState" in first.extra


def test_parse_lot_record_basic_fields() -> None:
    rec = {
        "id": 301392528,
        "itemId": 1918110,
        "lotNumber": "201",
        "lead": "2015 Coachmen Viking Epic Tent Trailer",
        "description": "VIN: 5ZT1VKEC0F5009389; 21ft, Sleeps 5",
        "bidAmount": 800.0,
        "pictureCount": 35,
        "shippingOffered": False,
        "pictures": [
            {"fullSizeLocation": "https://cdn.hibid.com/img.axd?id=1"},
            {"fullSizeLocation": "https://cdn.hibid.com/img.axd?id=2"},
        ],
        "lotState": {
            "highBid": 800.0,
            "minBid": 850.0,
            "bidCount": 2,
            "isClosed": False,
            "isLive": False,
            "status": "OPEN",
        },
        "category": {"id": 150000, "categoryName": "RVs"},
    }
    summary = parse_lot_record(rec)
    assert summary.source_lot_id == "1918110"
    assert summary.lot_number == "201"
    assert summary.title == "2015 Coachmen Viking Epic Tent Trailer"
    assert summary.description == "VIN: 5ZT1VKEC0F5009389; 21ft, Sleeps 5"
    assert summary.current_high_bid_cad == Decimal("800.0")
    assert summary.bid_count_visible == 2
    expected_photo_count = 2
    assert len(summary.photos) == expected_photo_count
    assert summary.photos[0].startswith("https://")
    # auction_external_id and url come from the caller's LotRef, not the lot record.
    assert summary.auction_external_id is None
    assert summary.url is None


def test_parse_lot_record_zero_high_bid_returns_none() -> None:
    """High-bid 0.0 means 'no bid yet' — the parser folds to None so
    downstream renderers don't show $0 bids."""
    rec = {
        "itemId": 1,
        "lotNumber": "1",
        "lead": "Test",
        "pictures": [],
        "lotState": {"highBid": 0.0, "bidCount": 0, "isClosed": False},
    }
    summary = parse_lot_record(rec)
    assert summary.current_high_bid_cad is None


def test_parse_lot_record_missing_lot_state_does_not_crash() -> None:
    """A record without lotState is malformed but should not raise — the
    parser must produce a HibidLotSummary with None for bid/count."""
    rec = {"itemId": 42, "lotNumber": "1", "lead": "Test"}
    summary = parse_lot_record(rec)
    assert summary.current_high_bid_cad is None
    assert summary.bid_count_visible is None


def test_lot_is_closed_open_lot() -> None:
    assert lot_is_closed({"lotState": {"isClosed": False}}) is False


def test_lot_is_closed_closed_lot() -> None:
    assert lot_is_closed({"lotState": {"isClosed": True}}) is True


def test_lot_is_closed_no_lot_state() -> None:
    assert lot_is_closed({}) is False


# ── auction details (GraphQL response) ───────────────────────────────────


def test_parse_auction_details_against_live_fixture() -> None:
    data = json.loads((FIX / "auction_details.json").read_text())
    det = parse_auction_details_response(data)
    assert det.auction_id == "741423"
    # The captured auction is by Graham Auctions; confirm the auctioneer
    # parsing isn't dropping the name field.
    assert det.auctioneer_name  # non-empty
    # bid_close_at should be an aware datetime if present.
    if det.bid_close_at is not None:
        assert det.bid_close_at.tzinfo is not None


def test_parse_auction_details_minimal_synthetic() -> None:
    data: dict[str, dict[str, dict[str, object]]] = {
        "data": {
            "auction": {
                "id": 999,
                "eventName": "Test Auction",
                "description": "desc",
                "bidOpenDateTime": "2026-05-15T10:00:00Z",
                "bidCloseDateTime": "2026-05-16T18:00:00Z",
                "eventAddress": "123 Main",
                "eventCity": "Calgary",
                "eventState": "AB",
                "buyerPremiumRate": 0.10,
                "auctioneer": {
                    "id": 7,
                    "name": "Test Auctioneer",
                },
            },
        },
    }
    det = parse_auction_details_response(data)
    assert det.auction_id == "999"
    assert det.event_name == "Test Auction"
    assert det.bid_open_at == datetime(2026, 5, 15, 10, 0, tzinfo=UTC)
    assert det.bid_close_at == datetime(2026, 5, 16, 18, 0, tzinfo=UTC)
    assert det.event_city == "Calgary"
    assert det.event_state == "AB"
    assert det.buyer_premium_pct == Decimal("0.10")
    assert det.auctioneer_name == "Test Auctioneer"
    assert det.auctioneer_external_id == "7"


def test_parse_auction_details_empty_data_returns_blank_details() -> None:
    det = parse_auction_details_response({"data": {"auction": None}})
    assert isinstance(det, HibidAuctionDetails)
    assert det.auction_id == ""
    assert det.event_name is None
