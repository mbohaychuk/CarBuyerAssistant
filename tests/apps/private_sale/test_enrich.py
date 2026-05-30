"""Tests for enrich_private_listing.

Uses a fake DescribeProvider — no real LLM calls.
Verifies:
  - DescribeInput is constructed with the private-sale fixed fields
    (auction_subtype="private", bid_increment=None, auction_close_at=None,
    auctioneer_name=None, image_count=len(photos), current_high_bid_cad=ask_price).
  - EnrichmentOutput is applied to the correct PrivateListing columns.
  - enrichment_status is set to "done".
  - AuctionLot-only fields (condition_confidence, llm_concerns, carfax_url,
    engine, transmission, drivetrain, desirability_signals, etc.) are NOT
    attempted — they don't exist on PrivateListing.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from carbuyer.apps.private_sale.enrich import enrich_private_listing
from carbuyer.db.models import PrivateListing
from carbuyer.llm.base import DescribeInput
from carbuyer.llm.schemas import (
    Concern,
    EnrichmentOutput,
    FlagInstance,
    NormalizedVehicle,
    RarityAssessment,
)

# Named constants to satisfy PLR2004 (no magic values in comparisons).
_LISTING_ID = 1
_PHOTO_COUNT = 2
_YEAR = 2018
_MILEAGE_KM = 95_000
_ASK_PRICE = Decimal("42500")
_VIN = "5TFDW5F18JX123456"


# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------

_CANNED_OUTPUT = EnrichmentOutput(
    normalized_vehicle=NormalizedVehicle(
        year=_YEAR,
        make="Toyota",
        model="Tundra",
        trim="TRD Pro",
        engine="5.7L V8",
        transmission="automatic",
        drivetrain="4wd",
        mileage_km=_MILEAGE_KM,
        mileage_is_verified=True,
        vin=_VIN,
    ),
    title_status="NORMAL",
    condition_categorical="good",
    condition_confidence=0.85,
    red_flags=[
        FlagInstance(flag="frame_rust", evidence="seller mentions surface rust", weight=3),
    ],
    green_flags=[
        FlagInstance(flag="service_records", evidence="full dealer service history", weight=2),
    ],
    showstopper_flags=[],
    concerns=[
        Concern(text="Minor rust on frame rails", severity="minor"),
    ],
    carfax_url=None,
    summary="Clean 2018 Tundra TRD Pro with full service history.",
    description_quality="detailed",
    rarity=RarityAssessment(
        desirable_trim_or_spec=True,
        classic_or_collector=False,
        desirability_signals=["TRD Pro trim"],
        desirability_evidence=["TRD Pro badge mentioned"],
    ),
)

_UNKNOWN_TITLE_OUTPUT = EnrichmentOutput(
    normalized_vehicle=NormalizedVehicle(
        year=_YEAR,
        make="Toyota",
        model="Tundra",
        trim=None,
        engine=None,
        transmission="unknown",
        drivetrain="unknown",
        mileage_km=None,
        mileage_is_verified=None,
        vin=None,
    ),
    title_status="UNKNOWN",
    condition_categorical="decent",
    condition_confidence=0.4,
    red_flags=[],
    green_flags=[],
    showstopper_flags=[],
    concerns=[],
    carfax_url=None,
    summary="",
    description_quality="thin",
    rarity=RarityAssessment(
        desirable_trim_or_spec=False,
        classic_or_collector=False,
        desirability_signals=[],
        desirability_evidence=[],
    ),
)


class FakeProvider:
    """Captures the DescribeInput passed to describe() for assertion."""

    def __init__(self) -> None:
        self.received: DescribeInput | None = None

    async def describe(self, payload: DescribeInput) -> EnrichmentOutput:
        self.received = payload
        return _CANNED_OUTPUT


class _UnknownTitleProvider:
    async def describe(self, payload: DescribeInput) -> EnrichmentOutput:
        return _UNKNOWN_TITLE_OUTPUT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_listing() -> PrivateListing:
    """In-memory PrivateListing with raw scraper values (no normalization yet)."""
    listing = PrivateListing()
    listing.id = _LISTING_ID
    listing.source = "kijiji"
    listing.source_listing_id = "test-001"
    listing.url = "https://www.kijiji.ca/v-cars-trucks/calgary/test/1234"
    listing.canonical_url = "https://www.kijiji.ca/v-cars-trucks/calgary/test/1234"
    listing.title = "2018 Toyota Tundra TRD Pro 4x4"
    listing.description = "Full service history. No accidents. Surface rust on frame."
    listing.photos = ["https://cdn.kijiji.ca/a.jpg", "https://cdn.kijiji.ca/b.jpg"]
    listing.year = None
    listing.make = None
    listing.model = None
    listing.trim = None
    listing.vin = None
    listing.mileage_km = None
    listing.ask_price_cad = _ASK_PRICE
    listing.pickup_province = "AB"
    listing.red_flags = []
    listing.green_flags = []
    listing.showstopper_flags = []
    listing.enrichment_status = "pending"
    listing.desirable_trim_or_spec = False
    listing.classic_or_collector = False
    listing.title_status = "UNKNOWN"
    return listing


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_describe_input_shape() -> None:
    """DescribeInput must carry the private-sale fixed values."""
    listing = _make_listing()
    fake = FakeProvider()

    await enrich_private_listing(listing, provider=fake)

    assert fake.received is not None
    inp = fake.received
    assert inp.auction_subtype == "private"
    assert inp.auctioneer_name is None
    assert inp.bid_increment is None
    assert inp.auction_close_at is None
    assert inp.is_no_reserve is False
    assert inp.current_high_bid_cad == _ASK_PRICE
    assert inp.image_count == _PHOTO_COUNT
    assert inp.lot_id == _LISTING_ID
    assert inp.title == "2018 Toyota Tundra TRD Pro 4x4"
    assert inp.pickup_province == "AB"


@pytest.mark.asyncio
async def test_image_count_no_photos() -> None:
    """image_count=0 when photos is empty."""
    listing = _make_listing()
    listing.photos = []
    fake = FakeProvider()

    await enrich_private_listing(listing, provider=fake)

    assert fake.received is not None
    assert fake.received.image_count == 0


@pytest.mark.asyncio
async def test_normalized_vehicle_applied() -> None:
    """make/model/year/trim/mileage_km/vin from normalized_vehicle are written."""
    listing = _make_listing()

    await enrich_private_listing(listing, provider=FakeProvider())

    assert listing.year == _YEAR
    assert listing.make == "Toyota"
    assert listing.model == "Tundra"
    assert listing.trim == "TRD Pro"
    assert listing.mileage_km == _MILEAGE_KM
    assert listing.vin == _VIN


@pytest.mark.asyncio
async def test_enrichment_fields_applied() -> None:
    """title_status, condition, flags, summary, rarity booleans all written."""
    listing = _make_listing()

    await enrich_private_listing(listing, provider=FakeProvider())

    assert listing.title_status == "NORMAL"
    assert listing.condition_categorical == "good"
    assert len(listing.red_flags) == 1
    assert listing.red_flags[0]["flag"] == "frame_rust"
    assert len(listing.green_flags) == 1
    assert listing.green_flags[0]["flag"] == "service_records"
    assert listing.showstopper_flags == []
    assert listing.summary == "Clean 2018 Tundra TRD Pro with full service history."
    assert listing.desirable_trim_or_spec is True
    assert listing.classic_or_collector is False


@pytest.mark.asyncio
async def test_enrichment_status_done() -> None:
    """enrichment_status is set to 'done' on success."""
    listing = _make_listing()

    await enrich_private_listing(listing, provider=FakeProvider())

    assert listing.enrichment_status == "done"


@pytest.mark.asyncio
async def test_title_status_unknown_not_applied() -> None:
    """UNKNOWN title_status must not overwrite a pre-existing real value."""
    listing = _make_listing()
    listing.title_status = "SALVAGE"  # pre-existing non-UNKNOWN value

    await enrich_private_listing(listing, provider=_UnknownTitleProvider())

    # UNKNOWN must not clobber "SALVAGE"
    assert listing.title_status == "SALVAGE"
