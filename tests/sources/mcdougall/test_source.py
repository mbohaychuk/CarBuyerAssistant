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
