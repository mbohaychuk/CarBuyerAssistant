from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from carbuyer.sources.hibid.parser import (
    extract_lot_models,
    parse_lot_summary,
    raw_lot_id,
)

FIXTURE_PATH = Path("tests/sources/fixtures/hibid_catalog_synthetic.html")


def test_extract_lot_models_handles_nested_arrays() -> None:
    html = """
    <html><body><script>
    var lotModels = [
      {"eventItemId": 1, "lead": "Truck A", "images": ["a.jpg", "b.jpg"]},
      {"eventItemId": 2, "lead": "Truck B", "images": []}
    ];
    </script></body></html>
    """
    lots = extract_lot_models(html)
    expected_count = 2
    assert len(lots) == expected_count
    assert lots[0]["eventItemId"] == 1
    assert lots[0]["images"] == ["a.jpg", "b.jpg"]


def test_extract_lot_models_no_match() -> None:
    assert extract_lot_models("<html>no script here</html>") == []


def test_extract_lot_models_handles_string_with_brackets() -> None:
    # Strings containing brackets must NOT confuse the bracket scanner.
    html = 'var lotModels = [{"lead": "Hood [scratched]", "eventItemId": 7}];'
    lots = extract_lot_models(html)
    assert lots == [{"lead": "Hood [scratched]", "eventItemId": 7}]


def test_parse_lot_summary_real_keys() -> None:
    raw = {
        "eventItemId": 4242,
        "lotNumber": "12B",
        "lead": "1995 Ford F-150 4x4",
        "description": "runs and drives",
        "lotUrl": "/alberta/lot/300724160/1995-ford-f150",
        "lotStatus": {
            "highBid": "1500.00",
            "bidCount": 7,
        },
        "auctionEnd": "2026-06-01T18:30:00Z",
        "auctionId": 740236,
        "bidIncrement": 25,
        "reserveStatus": "no_reserve",
        "lotState": "open",
    }
    summary = parse_lot_summary(raw)
    assert summary.source_lot_id == "4242"
    assert summary.lot_number == "12B"
    assert summary.title == "1995 Ford F-150 4x4"
    assert summary.description == "runs and drives"
    assert summary.current_high_bid_cad == Decimal("1500.00")
    expected_bid_count = 7
    assert summary.bid_count_visible == expected_bid_count
    assert summary.url == "/alberta/lot/300724160/1995-ford-f150"
    assert summary.auction_external_id == "740236"
    assert summary.end_at == datetime(2026, 6, 1, 18, 30, 0, tzinfo=UTC)
    # Year/make/model are NOT separate fields in HiBid; left None for enricher.
    assert summary.year is None
    assert summary.make is None
    expected_increment = 25
    assert summary.extra["bidIncrement"] == expected_increment
    assert summary.extra["reserveStatus"] == "no_reserve"
    assert summary.extra["lotState"] == "open"


def test_parse_lot_summary_handles_messy_currency() -> None:
    raw = {"eventItemId": 1, "lead": "x", "lotStatus": {"highBid": "$1,200.00"}}
    summary = parse_lot_summary(raw)
    assert summary.current_high_bid_cad == Decimal("1200.00")


def test_parse_lot_summary_handles_missing_lot_status() -> None:
    raw = {"eventItemId": 1, "lead": "x"}
    summary = parse_lot_summary(raw)
    assert summary.current_high_bid_cad is None
    assert summary.bid_count_visible is None


def test_raw_lot_id_falls_through_key_variants() -> None:
    assert raw_lot_id({"eventItemId": 5}) == "5"
    assert raw_lot_id({"lotId": 6}) == "6"
    assert raw_lot_id({"id": 7}) == "7"
    assert raw_lot_id({}) is None


def test_extract_lot_models_against_synthetic_fixture() -> None:
    if not FIXTURE_PATH.exists():
        pytest.skip(f"synthetic fixture missing: {FIXTURE_PATH}")
    html = FIXTURE_PATH.read_text()
    lots = extract_lot_models(html)
    expected_min = 2
    assert len(lots) >= expected_min
    s = parse_lot_summary(lots[0])
    assert s.source_lot_id
    assert s.title is not None
