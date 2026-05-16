"""Tests for HibidSource (post-SPA migration).

Discovery still uses SSR'd HTML; everything else hits GraphQL. The mock
transport routes by request URL + GraphQL operationName so tests
exercise the real dispatch in source.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from carbuyer.sources.base import SOURCES, AuctionRef, LotRef
from carbuyer.sources.hibid.source import HibidSource

FIX = Path("tests/sources/hibid/fixtures")
EXPECTED_LIVE_LOT_COUNT = 79


_DISCOVER_SYNTH = """<html><body>
  <a href="/alberta/auction/740236/spring-vehicle-event">Spring</a>
  <a href="/alberta/auction/740237/farm-dispersal">Farm</a>
  <a href="/lots/700006/cars-and-vehicles">Vehicles category (skip)</a>
</body></html>"""

_LOTS_TWO_RESPONSE: dict[str, Any] = {
    "data": {
        "lotSearch": {
            "pagedResults": {
                "pageLength": 100, "pageNumber": 1, "totalCount": 2, "filteredCount": 2,
                "results": [
                    {
                        "id": 30000001, "itemId": 4242, "lotNumber": "1",
                        "lead": "1995 Ford F-150 4x4",
                        "description": "runs and drives",
                        "bidAmount": 800.0, "pictureCount": 5, "shippingOffered": False,
                        "pictures": [{"fullSizeLocation": "https://x/1.jpg"}],
                        "lotState": {
                            "highBid": 800.0, "minBid": 850.0, "bidCount": 2,
                            "isClosed": False, "isLive": False, "status": "OPEN",
                        },
                        "category": {"id": 209, "categoryName": "Light Duty Trucks"},
                    },
                    {
                        "id": 30000002, "itemId": 4243, "lotNumber": "2",
                        "lead": "2018 Toyota Tacoma TRD",
                        "description": "trim package, runs great",
                        "bidAmount": 12000.0, "pictureCount": 8, "shippingOffered": False,
                        "pictures": [{"fullSizeLocation": "https://x/2.jpg"}],
                        "lotState": {
                            "highBid": 12000.0, "minBid": 12500.0, "bidCount": 8,
                            "isClosed": False, "isLive": False, "status": "OPEN",
                        },
                        "category": {"id": 40010, "categoryName": "Cars"},
                    },
                ],
            },
        },
    },
}

_LOTS_EMPTY_RESPONSE: dict[str, Any] = {
    "data": {
        "lotSearch": {
            "pagedResults": {
                "pageLength": 100, "pageNumber": 1, "totalCount": 0,
                "filteredCount": 0, "results": [],
            },
        },
    },
}

_AUCTION_DETAILS_RESPONSE: dict[str, Any] = {
    "data": {
        "auction": {
            "id": 740236,
            "eventName": "Spring Vehicle Event",
            "description": "Vehicles in Calgary",
            "bidOpenDateTime": "2026-05-15T10:00:00Z",
            "bidCloseDateTime": "2026-05-16T18:00:00Z",
            "eventAddress": "123 Main St",
            "eventCity": "Calgary",
            "eventState": "AB",
            "buyerPremiumRate": 0.10,
            "auctioneer": {"id": 1, "name": "Graham Auctions"},
        },
    },
}


def _mock_transport(
    *,
    discover_html: str = _DISCOVER_SYNTH,
    lots_response: dict[str, Any] | None = None,
    auction_response: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    """Route by URL + GraphQL operationName."""
    lots = lots_response if lots_response is not None else _LOTS_TWO_RESPONSE
    auction = auction_response if auction_response is not None else _AUCTION_DETAILS_RESPONSE

    async def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == "https://hibid.com/" or "/auctions/" in url:
            return httpx.Response(200, text=discover_html)
        if url.endswith("/graphql"):
            body = json.loads(req.content)
            op = body.get("operationName")
            if op == "AuctionDetails":
                return httpx.Response(200, json=auction)
            if op == "LotSearchLotOnly":
                return httpx.Response(200, json=lots)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


# ── registration / metadata ──────────────────────────────────────────────


def test_hibid_source_registered_at_import() -> None:
    assert "hibid" in SOURCES
    src = SOURCES["hibid"]
    assert src.name == "hibid"
    assert src.version  # any non-empty string


def test_hibid_source_has_classvar_name_and_version() -> None:
    assert HibidSource.name == "hibid"
    assert HibidSource.version


# ── discover ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_auctions_yields_unique_refs() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        refs = [r async for r in src.discover_auctions()]
    assert len(refs) >= 1
    assert all(r.source == "hibid" for r in refs)
    assert {r.source_auction_id for r in refs} == {"740236", "740237"}
    # Category-only links did NOT slip into the discovery results.


# ── fetch_auction ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_auction_populates_metadata() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = AuctionRef(
            source="hibid", source_auction_id="740236",
            url="https://hibid.com/catalog/740236",
        )
        raw = await src.fetch_auction(ref)
    assert raw.title == "Spring Vehicle Event"
    assert raw.auctioneer_name == "Graham Auctions"
    assert raw.pickup_city == "Calgary"
    assert raw.pickup_province == "AB"
    assert raw.scheduled_end_at is not None
    assert raw.scheduled_end_at.tzinfo is not None


# ── fetch_lots / fetch_lot ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_lots_yields_lot_refs() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = AuctionRef(
            source="hibid", source_auction_id="740236",
            url="https://hibid.com/catalog/740236",
        )
        lots = [r async for r in src.fetch_lots(ref)]
    expected_count = 2
    assert len(lots) == expected_count
    assert all(r.source_auction_id == "740236" for r in lots)
    assert {r.source_lot_id for r in lots} == {"4242", "4243"}


@pytest.mark.asyncio
async def test_fetch_lots_then_fetch_lot_uses_cache() -> None:
    """fetch_lots caches each record so fetch_lot can return immediately
    without a second GraphQL round trip. Test by exhausting the iterator
    then verifying fetch_lot returns the same data."""
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = AuctionRef(
            source="hibid", source_auction_id="740236",
            url="https://hibid.com/catalog/740236",
        )
        async for _ in src.fetch_lots(ref):
            pass
        lot_ref = LotRef(
            source="hibid", source_auction_id="740236",
            source_lot_id="4242", url="https://hibid.com/lot/30000001",
            source_lot_row_id=30000001,
        )
        raw = await src.fetch_lot(lot_ref)
    assert raw.title == "1995 Ford F-150 4x4"
    assert raw.current_high_bid_cad is not None
    assert "lotState" in raw.extra


@pytest.mark.asyncio
async def test_fetch_lot_cache_miss_falls_back_to_graphql() -> None:
    """If fetch_lot is called for an itemId NOT in cache, the source
    falls back to a single-lot GraphQL query so the call still succeeds."""
    single_lot_response = {
        "data": {"lotSearch": {"pagedResults": {
            "totalCount": 1, "results": [_LOTS_TWO_RESPONSE["data"]["lotSearch"]["pagedResults"]["results"][0]],
        }}},
    }
    async with HibidSource(
        provinces=["AB"], _transport=_mock_transport(lots_response=single_lot_response),
    ) as src:
        # Skip fetch_lots — go straight to fetch_lot to force cache miss.
        lot_ref = LotRef(
            source="hibid", source_auction_id="740236",
            source_lot_id="4242", url="https://hibid.com/lot/30000001",
            source_lot_row_id=30000001,
        )
        raw = await src.fetch_lot(lot_ref)
    assert raw.title == "1995 Ford F-150 4x4"


# ── poll_bid ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_bid_returns_observation() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = LotRef(
            source="hibid", source_auction_id="740236",
            source_lot_id="4242", url="https://hibid.com/lot/30000001",
            source_lot_row_id=30000001,
        )
        obs = await src.poll_bid(ref)
    assert obs.current_high_bid_cad is not None
    assert obs.status_at_observation == "open"
    assert obs.observed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_poll_bid_missing_lot_returns_missing_status() -> None:
    """When the GraphQL response has no results (lot deleted / unknown id),
    poll_bid returns status='missing'. The downstream bid_poller already
    treats missing as 'close out the lot' — see _write_observation."""
    async with HibidSource(
        provinces=["AB"], _transport=_mock_transport(lots_response=_LOTS_EMPTY_RESPONSE),
    ) as src:
        ref = LotRef(
            source="hibid", source_auction_id="740236",
            source_lot_id="999999", url="https://hibid.com/lot/999999",
            source_lot_row_id=999999,
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "missing"
    assert obs.current_high_bid_cad is None


@pytest.mark.asyncio
async def test_poll_bid_closed_lot_status() -> None:
    closed_response: dict[str, Any] = {
        "data": {"lotSearch": {"pagedResults": {
            "totalCount": 1,
            "results": [{
                "id": 1, "itemId": 4242, "lotNumber": "1", "lead": "Sold lot",
                "pictures": [],
                "lotState": {
                    "highBid": 9500.0, "bidCount": 12, "isClosed": True,
                    "isLive": False, "status": "CLOSED",
                },
            }],
        }}},
    }
    async with HibidSource(
        provinces=["AB"], _transport=_mock_transport(lots_response=closed_response),
    ) as src:
        ref = LotRef(
            source="hibid", source_auction_id="740236",
            source_lot_id="4242", url="https://hibid.com/lot/1",
            source_lot_row_id=1,
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "closed"
    assert obs.current_high_bid_cad is not None


# ── lifecycle ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_using_source_outside_context_manager_raises() -> None:
    src = HibidSource(provinces=["AB"])
    with pytest.raises(RuntimeError, match="outside"):
        await src.fetch_auction(
            AuctionRef(
                source="hibid", source_auction_id="100",
                url="https://hibid.com/catalog/100",
            ),
        )


# ── live-fixture smoke: full-page response parses without crashing ──────


def test_lot_search_live_fixture_parses_without_errors() -> None:
    """The full captured response (79 lots, 1MB) must parse cleanly through
    the source's parsing path. This is the canary for HiBid schema drift —
    if HiBid renames a field or changes a shape, this catches it."""
    from carbuyer.sources.hibid.parser import parse_lot_search_response
    data = json.loads((FIX / "lot_search.json").read_text())
    summaries = parse_lot_search_response(data)
    assert len(summaries) == EXPECTED_LIVE_LOT_COUNT
    # Each summary has a non-empty itemId-derived source_lot_id.
    assert all(s.source_lot_id for s in summaries)


# ── discover_vehicle_lots (cross-auction LotSearch) ──────────────────────


@pytest.mark.asyncio
async def test_discover_vehicle_lots_yields_auction_lot_pairs_from_live_fixture() -> None:
    """Walk the captured 100-lot cross-auction response and verify each
    yielded pair has both a RawAuction (derived from the embedded auction
    sub-selection) and a RawLot. No second AuctionDetails round-trip
    required — that's the whole point of the lot-first ingest model."""
    fixture = json.loads((FIX / "lot_search_cross_auction.json").read_text())

    def page_response(page: int) -> dict[str, Any]:
        # Single-page response for the test: return the fixture only on
        # pageNumber=1, then terminate by returning empty results.
        if page == 1:
            return fixture
        return {"data": {"lotSearch": {"pagedResults": {
            "pageLength": 100, "pageNumber": page,
            "totalCount": 100, "filteredCount": 100, "results": [],
        }}}}

    async def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == "https://hibid.com/" or "/auctions/" in url:
            return httpx.Response(200, text="")
        if url.endswith("/graphql"):
            body = json.loads(req.content)
            page = body.get("variables", {}).get("pageNumber", 1)
            return httpx.Response(200, json=page_response(page))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with HibidSource(provinces=["AB"], _transport=transport) as src:
        pairs = [pair async for pair in src.discover_vehicle_lots("AB")]
    expected_page1_count = 100
    assert len(pairs) == expected_page1_count
    for auction, lot in pairs:
        assert auction.ref.source == "hibid"
        assert auction.ref.source_auction_id.isdigit()
        assert lot.ref.source_lot_id.isdigit()
        # Auction sub-selection populates pickup_province + auctioneer_name.
        assert auction.pickup_province  # Alberta auctions have this set.
        assert auction.scheduled_end_at is not None


@pytest.mark.asyncio
async def test_discover_vehicle_lots_skips_records_missing_auction_or_itemid() -> None:
    """Defensive: a malformed record without the embedded auction or itemId
    is dropped silently. The lot-first ingest cannot upsert a row without
    a parent auction row, so we'd rather skip than partial-insert."""
    response = {"data": {"lotSearch": {"pagedResults": {
        "pageLength": 100, "pageNumber": 1,
        "totalCount": 3, "filteredCount": 3,
        "results": [
            # Valid — yielded.
            {
                "id": 1, "itemId": 100, "lotNumber": "1", "lead": "Valid",
                "pictures": [], "lotState": {"highBid": 0, "isClosed": False},
                "auction": {
                    "id": 999, "eventName": "Auction A",
                    "bidCloseDateTime": "2026-06-01T18:00:00Z",
                    "eventCity": "Calgary", "eventState": "AB",
                    "buyerPremiumRate": 0.10,
                    "auctioneer": {"id": 1, "name": "Test"},
                },
            },
            # Missing auction — skipped.
            {"id": 2, "itemId": 200, "lead": "no auction", "pictures": []},
            # Missing itemId — skipped.
            {
                "id": 3, "lotNumber": "3", "lead": "no itemId",
                "pictures": [], "lotState": {"highBid": 0, "isClosed": False},
                "auction": {"id": 999, "auctioneer": {"id": 1, "name": "Test"}},
            },
        ],
    }}}}

    async def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == "https://hibid.com/" or "/auctions/" in url:
            return httpx.Response(200, text="")
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(handler)
    async with HibidSource(provinces=["AB"], _transport=transport) as src:
        pairs = [pair async for pair in src.discover_vehicle_lots("AB")]
    assert len(pairs) == 1
    assert pairs[0][1].ref.source_lot_id == "100"
