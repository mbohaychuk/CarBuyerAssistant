from pathlib import Path

import httpx
import pytest

from carbuyer.sources.base import SOURCES, AuctionRef, LotRef
from carbuyer.sources.hibid.source import HibidSource

FIXTURE = Path("tests/sources/fixtures/hibid_catalog_synthetic.html").read_text()
EXPECTED_INCREMENT = 25
EXPECTED_LOT_COUNT = 2


def _mock_transport(*, body: str = FIXTURE, status: int = 200) -> httpx.MockTransport:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


def test_hibid_source_registered_at_import() -> None:
    assert "hibid" in SOURCES
    src = SOURCES["hibid"]
    assert src.name == "hibid"
    assert src.version  # any non-empty string


def test_hibid_source_has_classvar_name_and_version() -> None:
    assert HibidSource.name == "hibid"
    assert HibidSource.version


@pytest.mark.asyncio
async def test_discover_auctions_yields_unique_refs() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        refs = [ref async for ref in src.discover_auctions()]
    assert len(refs) > 0
    assert all(r.source == "hibid" for r in refs)
    assert len({r.source_auction_id for r in refs}) == len(refs)


@pytest.mark.asyncio
async def test_fetch_lots_yields_lot_refs() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = AuctionRef(
            source="hibid",
            source_auction_id="740236",
            url="https://hibid.com/catalog/740236",
        )
        lots = [r async for r in src.fetch_lots(ref)]
    assert len(lots) >= EXPECTED_LOT_COUNT
    assert all(r.source_auction_id == "740236" for r in lots)


@pytest.mark.asyncio
async def test_fetch_lot_returns_raw_lot_with_extra() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = LotRef(
            source="hibid",
            source_auction_id="740236",
            source_lot_id="4242",
            url="https://hibid.com/lot/4242",
        )
        raw = await src.fetch_lot(ref)
    assert raw.title is not None
    assert "Ford" in raw.title
    assert raw.extra.get("bidIncrement") == EXPECTED_INCREMENT
    assert raw.extra.get("reserveStatus") == "no_reserve"


@pytest.mark.asyncio
async def test_poll_bid_returns_observation() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = LotRef(
            source="hibid",
            source_auction_id="740236",
            source_lot_id="4242",
            url="https://hibid.com/lot/4242",
        )
        obs = await src.poll_bid(ref)
    assert obs.current_high_bid_cad is not None
    assert obs.observed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_poll_bid_missing_lot_returns_missing_status() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = LotRef(
            source="hibid",
            source_auction_id="740236",
            source_lot_id="999999",
            url="https://hibid.com/lot/999999",
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "missing"
    assert obs.current_high_bid_cad is None


@pytest.mark.asyncio
async def test_poll_bid_404_returns_missing_not_raises() -> None:
    """HiBid drops closed-lot URLs from the catalog and serves 404. Without
    treating that as 'missing', the bid-poller catches the raise_for_status
    exception, logs, and leaves the lot OPEN — the scheduler then re-polls it
    every 30s indefinitely."""
    http_not_found = 404
    async with HibidSource(
        provinces=["AB"], _transport=_mock_transport(body="", status=http_not_found),
    ) as src:
        ref = LotRef(
            source="hibid",
            source_auction_id="740236",
            source_lot_id="999999",
            url="https://hibid.com/lot/999999",
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "missing"
    assert obs.current_high_bid_cad is None


@pytest.mark.asyncio
async def test_using_source_outside_context_manager_raises() -> None:
    src = HibidSource(provinces=["AB"])
    with pytest.raises(RuntimeError, match="outside"):
        await src.fetch_auction(
            AuctionRef(source="hibid", source_auction_id="x", url="https://hibid.com/catalog/x"),
        )
