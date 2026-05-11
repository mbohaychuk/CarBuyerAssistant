from __future__ import annotations

import httpx
import pytest

import carbuyer.sources.base
from carbuyer.sources.base import AuctionRef, LotRef
from carbuyer.sources.mcdougall.source import McDougallSource  # registers source at import

# ── URL recognition ──────────────────────────────────────────────────────────


def test_parse_auction_url_recognizes_mcdougall_url() -> None:
    url = "https://www.mcdougallauction.com/auction/12345/test-slug"
    ref = McDougallSource.parse_auction_url(url)
    assert ref is not None
    assert ref.source == "mcdougall"
    assert ref.source_auction_id == "12345"


def test_parse_auction_url_recognizes_url_without_slug() -> None:
    url = "https://www.mcdougallauction.com/auction/99"
    ref = McDougallSource.parse_auction_url(url)
    assert ref is not None
    assert ref.source_auction_id == "99"


def test_parse_auction_url_returns_none_for_non_mcdougall() -> None:
    assert McDougallSource.parse_auction_url("https://hibid.com/catalog/740236") is None
    assert McDougallSource.parse_auction_url("https://example.com/auction/1") is None
    assert McDougallSource.parse_auction_url("") is None


def test_parse_auction_url_canonicalizes() -> None:
    url_with_tracking = (
        "https://www.mcdougallauction.com/auction/12345?utm_source=email&utm_medium=cpc"
    )
    ref = McDougallSource.parse_auction_url(url_with_tracking)
    assert ref is not None
    # Tracking params stripped; no fragment; host lowercased.
    assert "utm_source" not in ref.url
    assert "utm_medium" not in ref.url
    assert ref.source_auction_id == "12345"


# ── Registration ─────────────────────────────────────────────────────────────


def test_source_self_registers_on_import() -> None:
    assert "mcdougall" in carbuyer.sources.base.SOURCES


def test_source_version_set() -> None:
    assert McDougallSource.version == "1"


# ── Context manager guard ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_using_source_outside_context_manager_raises() -> None:
    src = McDougallSource()
    with pytest.raises(RuntimeError, match="outside"):
        await src.fetch_auction(
            AuctionRef(
                source="mcdougall",
                source_auction_id="1",
                url="https://www.mcdougallauction.com/auction/1",
            ),
        )


# ── HTTP wiring (MockTransport) ───────────────────────────────────────────────


def _mock_transport(*, status: int = 200, body: str = "") -> httpx.MockTransport:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


def _body_transport(body: str, *, status: int = 200) -> httpx.MockTransport:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_discover_auctions_parses_absolute_and_relative_hrefs() -> None:
    # Real-world hrefs come in both shapes; both must reconstruct to a
    # canonical absolute URL. Dedup on (auction_id) — duplicate links to the
    # same auction collapse to one ref.
    body = """
    <html><body>
      <a href="https://www.mcdougallauction.com/auction/12345/spring-sale">Abs</a>
      <a href="/auction/67890/summer-dispersal">Relative</a>
      <a href="/auction/12345/duplicate-slug">Dup of 12345</a>
      <a href="/about">Internal nav</a>
      <a href="/auction/nope">Non-numeric id</a>
    </body></html>
    """
    async with McDougallSource(_transport=_body_transport(body)) as src:
        refs = [ref async for ref in src.discover_auctions()]

    ids = sorted(r.source_auction_id for r in refs)
    assert ids == ["12345", "67890"]
    urls = {r.url for r in refs}
    # Relative href reconstructed; canonical form drops trailing junk.
    assert any("/auction/12345" in u for u in urls)
    assert any("/auction/67890" in u for u in urls)


@pytest.mark.asyncio
async def test_fetch_lots_parses_relative_lot_hrefs() -> None:
    body = """
    <html><body>
      <a href="/lot/111/red-truck">Lot 1</a>
      <a href="https://www.mcdougallauction.com/lot/222">Lot 2 absolute</a>
      <a href="/lot/notnumeric">Non-numeric lot id</a>
      <a href="/lot/">Empty lot id</a>
    </body></html>
    """
    async with McDougallSource(_transport=_body_transport(body)) as src:
        ref = AuctionRef(
            source="mcdougall",
            source_auction_id="12345",
            url="https://www.mcdougallauction.com/auction/12345",
        )
        lot_refs = [lot async for lot in src.fetch_lots(ref)]

    ids = sorted(lr.source_lot_id for lr in lot_refs)
    assert ids == ["111", "222"]
    for lr in lot_refs:
        assert lr.source_auction_id == "12345"
        assert lr.url.startswith("https://www.mcdougallauction.com/lot/")


@pytest.mark.asyncio
async def test_fetch_auction_returns_raw_auction() -> None:
    async with McDougallSource(_transport=_mock_transport()) as src:
        ref = AuctionRef(
            source="mcdougall",
            source_auction_id="12345",
            url="https://www.mcdougallauction.com/auction/12345",
        )
        auction = await src.fetch_auction(ref)
    assert auction.ref == ref
    assert auction.auctioneer_name == "McDougall Auctioneers"


@pytest.mark.asyncio
async def test_fetch_lot_returns_raw_lot() -> None:
    async with McDougallSource(_transport=_mock_transport()) as src:
        ref = LotRef(
            source="mcdougall",
            source_auction_id="12345",
            source_lot_id="999",
            url="https://www.mcdougallauction.com/lot/999",
        )
        lot = await src.fetch_lot(ref)
    assert lot.ref == ref
    assert lot.lot_status == "open"


@pytest.mark.asyncio
async def test_poll_bid_404_returns_missing() -> None:
    async with McDougallSource(_transport=_mock_transport(status=404)) as src:
        ref = LotRef(
            source="mcdougall",
            source_auction_id="12345",
            source_lot_id="999",
            url="https://www.mcdougallauction.com/lot/999",
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "missing"
    assert obs.current_high_bid_cad is None


@pytest.mark.asyncio
async def test_poll_bid_200_returns_open() -> None:
    async with McDougallSource(_transport=_mock_transport(status=200)) as src:
        ref = LotRef(
            source="mcdougall",
            source_auction_id="12345",
            source_lot_id="999",
            url="https://www.mcdougallauction.com/lot/999",
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "open"
    assert obs.observed_at.tzinfo is not None
