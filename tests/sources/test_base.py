import dataclasses
import inspect

import pytest

from carbuyer.sources.base import (
    SOURCES,
    AuctionDiscoverer,
    AuctionFetcher,
    AuctionRef,
    AuctionSource,
    BidPoller,
    LotRef,
    RawLot,
    Source,
    register,
)


def test_auction_ref_is_constructible_and_frozen() -> None:
    ref = AuctionRef(source="hibid", source_auction_id="A123", url="https://x")
    assert ref.source == "hibid"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.source = "other"  # type: ignore[misc]


def test_raw_lot_has_extra_escape_hatch() -> None:
    raw = RawLot(
        ref=LotRef(
            source="hibid", source_auction_id="A1", source_lot_id="L1", url="https://x",
        ),
        lot_number="1",
        title="Truck",
        description=None,
    )
    assert raw.extra == {}
    raw.extra["carfax_url"] = "https://carfax/abc"
    assert raw.extra["carfax_url"].endswith("abc")


def test_role_abcs_are_abstract() -> None:
    assert inspect.isabstract(AuctionDiscoverer)
    assert inspect.isabstract(AuctionFetcher)
    assert inspect.isabstract(BidPoller)
    assert inspect.isabstract(AuctionSource)


def test_register_adds_source_to_registry() -> None:
    # Save and restore so other tests' SOURCES entries (e.g. HibidSource
    # registered at import) survive.
    saved = dict(SOURCES)
    SOURCES.clear()
    try:
        class _StubSource(Source):
            name = "stub"
            version = "0.0.1"

        src = _StubSource()
        register(src)
        assert SOURCES["stub"] is src
    finally:
        SOURCES.clear()
        SOURCES.update(saved)
