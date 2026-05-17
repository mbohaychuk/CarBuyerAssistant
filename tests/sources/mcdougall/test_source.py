from __future__ import annotations

import pytest

import carbuyer.sources.base
from carbuyer.sources.base import AuctionRef
from carbuyer.sources.mcdougall.source import McDougallSource  # registers source at import

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
    # against the lot_scraper's parser_version cascade so stale rows re-pend.
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


# Stub-behaviour tests for discover_auctions / fetch_lots / fetch_lot /
# poll_bid were removed in this commit. Real fixture-based coverage lands in
# the upcoming catalog walker / lot-detail / poll_bid commits.
