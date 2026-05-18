from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import carbuyer.sources.base
from carbuyer.sources.base import AuctionRef, LotRef
from carbuyer.sources.mcdougall.parser import parse_catalog_page, parse_lot_detail
from carbuyer.sources.mcdougall.source import McDougallSource  # registers source at import

_FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Real McDougall auction-event URL shape:
#   https://www.mcdougallauction.com/auction-event.php?arg=<GUID-8-4-4-4-12>
# A GUID from the live site, captured 2026-05-16.
_GUID = "BD725BF6-AD5D-4186-A5CE-1F5B8BCF2918"
_AUCTION_URL = f"https://www.mcdougallauction.com/auction-event.php?arg={_GUID}"

# ── URL recognition ──────────────────────────────────────────────────────────


def test_parse_auction_url_recognizes_mcdougall_url() -> None:
    ref = McDougallSource.parse_auction_url(_AUCTION_URL)
    assert ref is not None
    assert ref.source == "mcdougall"
    assert ref.source_auction_id == _GUID


def test_parse_auction_url_recognizes_url_with_extra_query_params() -> None:
    url = f"https://www.mcdougallauction.com/auction-event.php?source=fag&arg={_GUID}"
    ref = McDougallSource.parse_auction_url(url)
    assert ref is not None
    assert ref.source_auction_id == _GUID


def test_parse_auction_url_returns_none_for_non_mcdougall() -> None:
    assert McDougallSource.parse_auction_url("https://hibid.com/catalog/740236") is None
    assert McDougallSource.parse_auction_url("https://example.com/auction/1") is None
    assert McDougallSource.parse_auction_url("") is None


def test_parse_auction_url_returns_none_when_arg_is_not_a_guid() -> None:
    # Strict GUID-only id pattern means a malformed arg= value doesn't slip
    # through as a fake auction id. Fails closed.
    url = "https://www.mcdougallauction.com/auction-event.php?arg=not-a-guid"
    assert McDougallSource.parse_auction_url(url) is None


def test_parse_auction_url_canonicalizes() -> None:
    url_with_tracking = (
        f"https://www.mcdougallauction.com/auction-event.php"
        f"?arg={_GUID}&utm_source=email&utm_medium=cpc"
    )
    ref = McDougallSource.parse_auction_url(url_with_tracking)
    assert ref is not None
    assert "utm_source" not in ref.url
    assert "utm_medium" not in ref.url
    assert ref.source_auction_id == _GUID


# ── Registration ─────────────────────────────────────────────────────────────


def test_source_self_registers_on_import() -> None:
    assert "mcdougall" in carbuyer.sources.base.SOURCES


def test_source_version_set() -> None:
    # Version is bumped when the discovery/fetch contract changes; checked
    # against AuctionLot.parser_version in the upsert cascade so stale rows
    # re-pend (see carbuyer.db.upserts.upsert_lot_with_status_cascade).
    assert McDougallSource.version == "1"


# ── Context manager guard ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_using_source_outside_context_manager_raises() -> None:
    src = McDougallSource()
    with pytest.raises(RuntimeError, match="outside"):
        await src.fetch_auction(
            AuctionRef(
                source="mcdougall",
                source_auction_id=_GUID,
                url=_AUCTION_URL,
            ),
        )


# ── Catalog parser ───────────────────────────────────────────────────────────


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def test_parse_catalog_page_extracts_all_lot_cards() -> None:
    # Real fixture captured 2026-05-16: products.php?category=Vehicles page 1.
    # The page renders 14 .auction-product-item cards inline.
    entries = parse_catalog_page(_load_fixture("vehicles_catalog_page1.html"))
    assert len(entries) == 14  # noqa: PLR2004


def test_parse_catalog_page_first_entry_has_expected_shape() -> None:
    # 1987 Chevrolet Monte Carlo — first card on the captured fixture.
    # If this snapshot ever drifts the test should fail loudly; the catalog
    # ordering (closing-asc) is stable enough that a fixture refresh would
    # be the explicit corrective action, not a tolerant test.
    entries = parse_catalog_page(_load_fixture("vehicles_catalog_page1.html"))
    e = entries[0]
    assert e.lot_guid == "36CEF055-34AB-477E-B0F9-80ACDA6EAA17"
    assert e.lot_url == (
        "https://www.mcdougallauction.com/products-full-view.php"
        "?arg=36CEF055-34AB-477E-B0F9-80ACDA6EAA17"
    )
    assert "Chevrolet Monte Carlo" in e.title
    assert e.location == "800 North Service Road, Emerald Park, SK"
    assert e.lot_number == "4"
    assert e.status_raw == "Open"
    assert e.current_high_bid_cad == Decimal("5400.00")
    assert e.scheduled_end_at is not None
    assert e.scheduled_end_at.tzinfo is not None
    # Stored UTC; the fixture's hidden input has 2026-05-25T18:00:00Z.
    assert e.scheduled_end_at.year == 2026  # noqa: PLR2004
    assert e.scheduled_end_at.tzinfo.utcoffset(e.scheduled_end_at) == UTC.utcoffset(
        e.scheduled_end_at,
    )
    assert e.photo_url is not None and e.photo_url.startswith("http")


def test_parse_catalog_page_handles_missing_current_bid() -> None:
    # Synthetic minimal lot card with no .current-bid div. The parser must
    # surface current_high_bid_cad=None rather than crashing or defaulting
    # to a phantom number.
    html = """
    <html><body>
      <div class="auction-product-item">
        <div class="item-img">
          <a href="products-full-view.php?arg=00000000-0000-4000-8000-000000000001">
            <img src="https://example.com/img.jpg">
          </a>
        </div>
        <div class="item-title"><div class="blockTextWrap"><div class="blockText">
          <h4><a href="products-full-view.php?arg=00000000-0000-4000-8000-000000000001">
            2020 Honda Civic</a></h4>
        </div></div></div>
        <div class="item-location"><p><span>Location:</span> Saskatoon, SK</p></div>
        <div class="lot-status"><p><span>Lot:</span> 1<span class="status">Status:</span> Open</p></div>
        <div class="close-date">
          <input type="hidden" id="txtLotEndDate00000000-0000-4000-8000-000000000001"
                 value="2026-06-01T18:00:00Z"/>
        </div>
      </div>
    </body></html>
    """  # noqa: E501
    entries = parse_catalog_page(html)
    assert len(entries) == 1
    assert entries[0].current_high_bid_cad is None
    assert entries[0].status_raw == "Open"


def test_parse_catalog_page_skips_card_without_lot_guid() -> None:
    # Defensive: malformed card (no products-full-view link) must NOT crash
    # the parse; it's logged and skipped.
    html = """
    <html><body>
      <div class="auction-product-item"><p>broken card with no link</p></div>
      <div class="auction-product-item">
        <a href="products-full-view.php?arg=00000000-0000-4000-8000-000000000002">
          <img src="">
        </a>
        <div class="item-title"><h4><a
          href="products-full-view.php?arg=00000000-0000-4000-8000-000000000002">
          2019 Ford F-150</a></h4></div>
      </div>
    </body></html>
    """
    entries = parse_catalog_page(html)
    assert len(entries) == 1
    assert entries[0].title == "2019 Ford F-150"


def test_parse_catalog_page_returns_empty_when_no_cards() -> None:
    # The pagination walker uses [] as the "no more pages" sentinel.
    assert parse_catalog_page("<html><body></body></html>") == []


# ── Catalog walker (pagination + HTTP) ───────────────────────────────────────


@pytest.fixture
def _no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace jittered_sleep (4-8s real wait) with a no-op so multi-page
    walker tests don't add tens of seconds per test. Real cadence is
    exercised in integration runs, not unit tests."""
    async def _noop() -> None:
        return None

    monkeypatch.setattr("carbuyer.sources.mcdougall.source.jittered_sleep", _noop)


@pytest.mark.asyncio
async def test_iter_catalog_entries_walks_pagination_until_empty(
    _no_jitter: None,
) -> None:
    # Page 1 returns 1 lot, page 2 returns empty (sentinel), walker stops.
    page1 = """
    <html><body>
      <div class="auction-product-item">
        <a href="products-full-view.php?arg=00000000-0000-4000-8000-000000000003">
          <img src="">
        </a>
        <div class="item-title"><h4><a
          href="products-full-view.php?arg=00000000-0000-4000-8000-000000000003">
          Test Lot</a></h4></div>
      </div>
    </body></html>
    """
    page2_empty = "<html><body></body></html>"

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "&p=2" in url:
            return httpx.Response(200, text=page2_empty)
        return httpx.Response(200, text=page1)

    transport = httpx.MockTransport(handler)
    async with McDougallSource(_transport=transport) as src:
        entries = [e async for e in src.iter_catalog_entries()]
    assert len(entries) == 1
    assert entries[0].lot_guid == "00000000-0000-4000-8000-000000000003"


# ── Lot detail parser ────────────────────────────────────────────────────────


def test_parse_lot_detail_extracts_all_fields_from_real_fixture() -> None:
    # Real fixture captured 2026-05-16: 1987 Chevrolet Monte Carlo, lot 4 of
    # the REGINA MONTHLY AG & INDUSTRIAL EQUIPMENT auction, GUID BD725BF6...
    html = _load_fixture("lot_detail_monte_carlo.html")
    d = parse_lot_detail(html, lot_guid="36CEF055-34AB-477E-B0F9-80ACDA6EAA17")

    assert d.title == "1987 Chevrolet Monte Carlo Luxury Sport Car"
    assert d.auction_guid == "BD725BF6-AD5D-4186-A5CE-1F5B8BCF2918"
    assert d.vin == "1G1GZ11H7HP127810"
    assert d.mileage_km == 47800  # noqa: PLR2004
    assert d.current_high_bid_cad == Decimal("5400")
    assert d.bid_count_visible == 31  # noqa: PLR2004
    assert d.scheduled_end_at is not None
    assert d.scheduled_end_at.year == 2026  # noqa: PLR2004
    assert d.pickup_location == "800 North Service Road, Emerald Park, SK"
    # 84 photos is the real captured count; if fixture is refreshed this
    # assertion intentionally needs an explicit update.
    assert len(d.photos) == 84  # noqa: PLR2004
    assert all(p.startswith("https://") for p in d.photos)
    assert d.description is not None
    assert "350cu.i" in d.description

    # Buyer premium parsed from page text, not hardcoded.
    assert d.buyer_premium_pct == Decimal("0.15")
    assert d.buyer_premium_max_cad == Decimal("2000")
    assert d.buyer_premium_min_cad == Decimal("20")

    # Extras: secondary fields not yet promoted to canonical RawLot columns.
    assert d.extras.get("engine") == "5.7L V8 Gas"
    assert d.extras.get("transmission") == "Automatic"
    assert d.extras.get("mileage_unverified") is True


def test_parse_lot_detail_handles_multi_item_serial_number() -> None:
    # Real production crash 2026-05-16: boat-with-trailer lot 6C95BAB0...
    # had a "Serial Number" field with three labeled IDs. Cramming that into
    # the 32-char vin column raised StringDataRightTruncation and aborted
    # the whole McDougall ingestion strategy. The parser must now:
    # - set vin=None for non-single-VIN values
    # - preserve the raw text in extras["raw_serial_number"] for inspection
    html = (
        "<html><body><p><strong>Serial Number:</strong>"
        " Trailer: 5KTBS1810LF528753  Boat: BLBX2184J920 Motor: 2B721768</p>"
        "</body></html>"
    )
    d = parse_lot_detail(html, lot_guid="00000000-0000-4000-8000-000000000099")
    assert d.vin is None
    assert d.extras.get("raw_serial_number") == (
        "Trailer: 5KTBS1810LF528753  Boat: BLBX2184J920 Motor: 2B721768"
    )


def test_parse_lot_detail_falls_back_when_buyer_premium_missing() -> None:
    # Defensive: if McDougall ever changes the terms paragraph, we fail
    # closed (None/None/None) so downstream all_in_cost uses defaults
    # rather than silently wrong values.
    html = "<html><body><div class='full-product-title'><h1>X</h1></div></body></html>"
    d = parse_lot_detail(html, lot_guid="00000000-0000-4000-8000-000000000000")
    assert d.buyer_premium_pct is None
    assert d.buyer_premium_max_cad is None
    assert d.buyer_premium_min_cad is None
    assert d.title == "X"


# ── discover_vehicle_lots (orchestrator) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_vehicle_lots_yields_auction_plus_lot_pairs(
    _no_jitter: None,
) -> None:
    # Tiny synthetic catalog with one lot; transport routes catalog page +
    # lot detail page to two different fixtures so the orchestrator wires
    # them together correctly. Page 2 returns empty -> walker stops.
    catalog_html = """
    <html><body>
      <div class="auction-product-item">
        <a href="products-full-view.php?arg=AAAAAAAA-1111-4222-8333-444444444444">
          <img src="">
        </a>
        <div class="item-title"><h4><a
          href="products-full-view.php?arg=AAAAAAAA-1111-4222-8333-444444444444">
          Test Lot</a></h4></div>
      </div>
    </body></html>
    """
    detail_html = """
    <html><body>
      <div class="full-product-title"><h1>Test Lot</h1></div>
      <p><span>Pick up location:</span> 100 Main St, Test City, AB</p>
      <a href="auction-event.php?arg=BBBBBBBB-2222-4333-8444-555555555555">parent</a>
      <span id="spnBidPrice">100</span>
      <input type="hidden" id="txtLotEndDateAAAAAAAA-1111-4222-8333-444444444444"
             value="2026-12-01T00:00:00Z"/>
      <p>Subject to <strong>15% Buyers Premium</strong> to a Max of $2000 per lot
         and a Minimum of $20 per lot.</p>
    </body></html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "products-full-view.php" in url:
            return httpx.Response(200, text=detail_html)
        if "&p=2" in url:
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(200, text=catalog_html)

    async with McDougallSource(_transport=httpx.MockTransport(handler)) as src:
        pairs = [pair async for pair in src.discover_vehicle_lots()]

    assert len(pairs) == 1
    raw_auction, raw_lot = pairs[0]
    # Auction resolved from the detail page's auction-event link.
    assert raw_auction.ref.source == "mcdougall"
    assert raw_auction.ref.source_auction_id == "BBBBBBBB-2222-4333-8444-555555555555"
    # Premium terms propagated from lot extras into auction columns.
    assert raw_auction.buyer_premium_pct == Decimal("0.15")
    assert raw_auction.buyer_premium_max_cad == Decimal("2000")
    assert raw_auction.buyer_premium_min_cad == Decimal("20")
    # Pickup location split: "100 Main St, Test City, AB".
    assert raw_auction.pickup_address == "100 Main St"
    assert raw_auction.pickup_city == "Test City"
    assert raw_auction.pickup_province == "AB"
    # Lot ref now carries the resolved auction GUID, not the placeholder.
    assert raw_lot.ref.source_auction_id == "BBBBBBBB-2222-4333-8444-555555555555"
    assert raw_lot.ref.source_lot_id == "AAAAAAAA-1111-4222-8333-444444444444"


# ── poll_bid ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_bid_open_lot_returns_current_bid_and_end_time() -> None:
    # Real fixture for an open lot — bid 5400, end 2026-05-25T18:00Z.
    html = _load_fixture("lot_detail_monte_carlo.html")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    async with McDougallSource(_transport=httpx.MockTransport(handler)) as src:
        ref = LotRef(
            source="mcdougall",
            source_auction_id="BD725BF6-AD5D-4186-A5CE-1F5B8BCF2918",
            source_lot_id="36CEF055-34AB-477E-B0F9-80ACDA6EAA17",
            url=(
                "https://www.mcdougallauction.com/products-full-view.php"
                "?arg=36CEF055-34AB-477E-B0F9-80ACDA6EAA17"
            ),
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "open"
    assert obs.current_high_bid_cad == Decimal("5400")
    assert obs.end_time_at_observation is not None
    assert obs.observed_at.tzinfo is not None  # UTC-aware


@pytest.mark.asyncio
async def test_poll_bid_404_returns_missing() -> None:
    # Defensive: a deleted lot must not crash the poller, must surface as
    # the "missing" status so the worker can mark it closed and stop polling.
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with McDougallSource(_transport=httpx.MockTransport(handler)) as src:
        ref = LotRef(
            source="mcdougall",
            source_auction_id="BD725BF6-AD5D-4186-A5CE-1F5B8BCF2918",
            source_lot_id="36CEF055-34AB-477E-B0F9-80ACDA6EAA17",
            url=(
                "https://www.mcdougallauction.com/products-full-view.php"
                "?arg=36CEF055-34AB-477E-B0F9-80ACDA6EAA17"
            ),
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "missing"
    assert obs.current_high_bid_cad is None
    assert obs.end_time_at_observation is None


@pytest.mark.asyncio
async def test_poll_bid_410_returns_missing() -> None:
    # 410 Gone is RFC-defined permanent removal; same operational meaning as
    # 404 for our purposes. If McDougall ever switches to (or adds) 410, the
    # poller must not raise_for_status and burn 30s polling slots until the
    # 24h force-close guard fires. Mirrors the HiBid 410 dispatch in
    # commit 24ddef1.
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, text="gone")

    async with McDougallSource(_transport=httpx.MockTransport(handler)) as src:
        ref = LotRef(
            source="mcdougall",
            source_auction_id="BD725BF6-AD5D-4186-A5CE-1F5B8BCF2918",
            source_lot_id="36CEF055-34AB-477E-B0F9-80ACDA6EAA17",
            url=(
                "https://www.mcdougallauction.com/products-full-view.php"
                "?arg=36CEF055-34AB-477E-B0F9-80ACDA6EAA17"
            ),
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "missing"
    assert obs.current_high_bid_cad is None
    assert obs.end_time_at_observation is None


# ── fetch_lot integration ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_lot_returns_raw_lot_from_detail_page() -> None:
    html = _load_fixture("lot_detail_monte_carlo.html")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    transport = httpx.MockTransport(handler)
    async with McDougallSource(_transport=transport) as src:
        ref = LotRef(
            source="mcdougall",
            source_auction_id="BD725BF6-AD5D-4186-A5CE-1F5B8BCF2918",
            source_lot_id="36CEF055-34AB-477E-B0F9-80ACDA6EAA17",
            url=(
                "https://www.mcdougallauction.com/products-full-view.php"
                "?arg=36CEF055-34AB-477E-B0F9-80ACDA6EAA17"
            ),
        )
        raw_lot = await src.fetch_lot(ref)

    # Canonical fields propagated.
    assert raw_lot.title == "1987 Chevrolet Monte Carlo Luxury Sport Car"
    assert raw_lot.vin == "1G1GZ11H7HP127810"
    assert raw_lot.mileage_km == 47800  # noqa: PLR2004
    assert raw_lot.current_high_bid_cad == Decimal("5400")
    assert raw_lot.bid_count_visible == 31  # noqa: PLR2004
    assert raw_lot.scheduled_end_at is not None
    assert len(raw_lot.photos) == 84  # noqa: PLR2004
    # Parent-auction GUID + per-auction premium terms ride along in extra
    # so the ingester can upsert the parent auction in the same pass.
    assert raw_lot.extra["auction_guid"] == "BD725BF6-AD5D-4186-A5CE-1F5B8BCF2918"
    assert raw_lot.extra["pickup_location"] == "800 North Service Road, Emerald Park, SK"
    assert raw_lot.extra["buyer_premium_pct"] == Decimal("0.15")
    assert raw_lot.extra["buyer_premium_max_cad"] == Decimal("2000")
    assert raw_lot.extra["buyer_premium_min_cad"] == Decimal("20")


@pytest.mark.asyncio
async def test_iter_catalog_entries_respects_max_pages_safety_cap(
    _no_jitter: None,
) -> None:
    # Defensive: if every page somehow returns content, max_pages keeps us
    # from looping forever. Walker stops after max_pages iterations even
    # without seeing an empty page.
    page = """
    <html><body>
      <div class="auction-product-item">
        <a href="products-full-view.php?arg=00000000-0000-4000-8000-000000000004">
          <img src="">
        </a>
        <div class="item-title"><h4><a
          href="products-full-view.php?arg=00000000-0000-4000-8000-000000000004">
          Repeated Lot</a></h4></div>
      </div>
    </body></html>
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    transport = httpx.MockTransport(handler)
    async with McDougallSource(_transport=transport) as src:
        entries = [
            e async for e in src.iter_catalog_entries(max_pages=3)
        ]
    # 3 pages * 1 lot each.
    assert len(entries) == 3  # noqa: PLR2004
