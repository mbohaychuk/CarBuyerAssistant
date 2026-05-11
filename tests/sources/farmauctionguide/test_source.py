from __future__ import annotations

import httpx
import pytest

import carbuyer.sources.base
from carbuyer.sources.base import AuctionRef, LotRef
from carbuyer.sources.farmauctionguide.source import (  # registers source at import
    FarmAuctionGuideSource,
    resolve_platform,
)

# ── resolve_platform ──────────────────────────────────────────────────────────


def test_resolve_platform_routes_hibid() -> None:
    url = "https://terrymcdougall.hibid.com/catalog/740236/some-slug"
    source, ext_id = resolve_platform(url)
    assert source == "hibid"
    assert ext_id == "740236"


def test_resolve_platform_routes_mcdougall() -> None:
    url = "https://www.mcdougallauction.com/auction/12345/regina-summer"
    source, ext_id = resolve_platform(url)
    assert source == "mcdougall"
    assert ext_id == "12345"


def test_resolve_platform_falls_back_to_unknown_with_host() -> None:
    url = "https://random-auctioneer.example.com/sale/abc"
    source, ext_id = resolve_platform(url)
    assert source == "unknown:random-auctioneer.example.com"
    assert ext_id == "abc"


def test_resolve_platform_strips_www() -> None:
    # www.hibid.com should still match the hibid rule.
    url = "https://www.hibid.com/catalog/700001"
    source, ext_id = resolve_platform(url)
    assert source == "hibid"
    assert ext_id == "700001"


def test_resolve_platform_handles_no_path_segment() -> None:
    url = "https://example.com/"
    source, ext_id = resolve_platform(url)
    assert source == "unknown:example.com"
    # When all path segments are empty after stripping the trailing slash,
    # the fallback uses the full URL as the id.
    assert ext_id == url


def test_resolve_platform_unknown_host_only_when_id_extraction_fails() -> None:
    # HiBid host but no /catalog/ or /auction/ in path — host matched but id failed.
    url = "https://hibid.com/some/other/page"
    source, _ext_id = resolve_platform(url)
    # Host matched hibid rule but id regex didn't match → falls through to unknown.
    assert source.startswith("unknown:")


# ── Registration and metadata ─────────────────────────────────────────────────


def test_source_self_registers_on_import() -> None:
    assert "farmauctionguide" in carbuyer.sources.base.SOURCES


def test_source_version_set() -> None:
    assert FarmAuctionGuideSource.version == "1"


def test_source_constructible_with_provinces() -> None:
    src = FarmAuctionGuideSource(provinces=["AB", "SK"])
    assert src.provinces == ["AB", "SK"]


# ── HTTP wiring helpers ───────────────────────────────────────────────────────


def _multi_transport(responses: dict[str, tuple[int, str]]) -> httpx.MockTransport:
    """MockTransport that dispatches to per-URL (status, body) pairs."""

    async def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for prefix, (status, body) in responses.items():
            if url_str.startswith(prefix):
                return httpx.Response(status, text=body)
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def _simple_transport(*, status: int = 200, body: str = "") -> httpx.MockTransport:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


# ── discover_auctions — routing ───────────────────────────────────────────────

_FIXTURE_AB = """
<html><body>
  <a href="https://terrymcdougall.hibid.com/catalog/111111/spring-sale">Auction 1</a>
  <a href="https://www.mcdougallauction.com/auction/22222/summer-dispersal">Auction 2</a>
  <a href="https://www.farmauctionguide.com/about/">About us</a>
</body></html>
"""


@pytest.mark.asyncio
async def test_discover_auctions_routes_known_platforms() -> None:
    transport = _multi_transport(
        {
            "https://www.farmauctionguide.com/canada/alberta/": (200, _FIXTURE_AB),
        }
    )
    async with FarmAuctionGuideSource(provinces=["AB"], _transport=transport) as src:
        refs = [ref async for ref in src.discover_auctions()]

    sources_seen = {(r.source, r.source_auction_id) for r in refs}
    assert ("hibid", "111111") in sources_seen
    assert ("mcdougall", "22222") in sources_seen


@pytest.mark.asyncio
async def test_discover_auctions_skips_farmauctionguide_internal_links() -> None:
    transport = _multi_transport(
        {
            "https://www.farmauctionguide.com/canada/alberta/": (200, _FIXTURE_AB),
        }
    )
    async with FarmAuctionGuideSource(provinces=["AB"], _transport=transport) as src:
        refs = [ref async for ref in src.discover_auctions()]

    # The /about/ link is internal — must not appear in refs.
    for ref in refs:
        assert "farmauctionguide" not in ref.url or ref.source.startswith("unknown:")


@pytest.mark.asyncio
async def test_discover_auctions_deduplicates() -> None:
    body = """
    <html><body>
      <a href="https://terrymcdougall.hibid.com/catalog/999/first">Link 1</a>
      <a href="https://terrymcdougall.hibid.com/catalog/999/duplicate">Link 2</a>
    </body></html>
    """
    transport = _multi_transport({"https://www.farmauctionguide.com/canada/alberta/": (200, body)})
    async with FarmAuctionGuideSource(provinces=["AB"], _transport=transport) as src:
        refs = [ref async for ref in src.discover_auctions()]

    hibid_refs = [r for r in refs if r.source == "hibid" and r.source_auction_id == "999"]
    assert len(hibid_refs) == 1


@pytest.mark.asyncio
async def test_discover_auctions_emits_unknown_for_unrecognized_platform() -> None:
    body = """
    <html><body>
      <a href="https://some-other-auction.example.com/sale/xyz123">Unknown</a>
    </body></html>
    """
    transport = _multi_transport({"https://www.farmauctionguide.com/canada/alberta/": (200, body)})
    async with FarmAuctionGuideSource(provinces=["AB"], _transport=transport) as src:
        refs = [ref async for ref in src.discover_auctions()]

    assert len(refs) == 1
    assert refs[0].source == "unknown:some-other-auction.example.com"


@pytest.mark.asyncio
async def test_discover_auctions_continues_on_province_failure() -> None:
    # AB fails (404), SK succeeds with one link.
    sk_body = """
    <html><body>
      <a href="https://terrymcdougall.hibid.com/catalog/333333/sk-sale">SK Auction</a>
    </body></html>
    """
    transport = _multi_transport(
        {
            "https://www.farmauctionguide.com/canada/alberta/": (404, ""),
            "https://www.farmauctionguide.com/canada/saskatchewan/": (200, sk_body),
        }
    )
    async with FarmAuctionGuideSource(provinces=["AB", "SK"], _transport=transport) as src:
        refs = [ref async for ref in src.discover_auctions()]

    assert any(r.source_auction_id == "333333" for r in refs)


# ── No-op methods ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_lots_yields_nothing() -> None:
    async with FarmAuctionGuideSource(provinces=[], _transport=_simple_transport()) as src:
        ref = AuctionRef(
            source="unknown:example.com",
            source_auction_id="abc",
            url="https://example.com/sale/abc",
        )
        lots = [lot async for lot in src.fetch_lots(ref)]
    assert lots == []


@pytest.mark.asyncio
async def test_fetch_lot_raises_not_implemented() -> None:
    async with FarmAuctionGuideSource(provinces=[], _transport=_simple_transport()) as src:
        ref = LotRef(
            source="unknown:example.com",
            source_auction_id="abc",
            source_lot_id="1",
            url="https://example.com/lot/1",
        )
        with pytest.raises(NotImplementedError):
            await src.fetch_lot(ref)


@pytest.mark.asyncio
async def test_poll_bid_returns_missing() -> None:
    async with FarmAuctionGuideSource(provinces=[], _transport=_simple_transport()) as src:
        ref = LotRef(
            source="unknown:example.com",
            source_auction_id="abc",
            source_lot_id="1",
            url="https://example.com/lot/1",
        )
        obs = await src.poll_bid(ref)
    assert obs.status_at_observation == "missing"
    assert obs.current_high_bid_cad is None
    assert obs.observed_at.tzinfo is not None


# ── Context manager guard ────────────────────────────────────────────────────


def test_using_source_outside_context_manager_raises() -> None:
    src = FarmAuctionGuideSource(provinces=["AB"])
    # _http property raises synchronously when no context manager is active.
    with pytest.raises(RuntimeError, match="outside"):
        _ = src._http  # type: ignore[reportPrivateUsage]
