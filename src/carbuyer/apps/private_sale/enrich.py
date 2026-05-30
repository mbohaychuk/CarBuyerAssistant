"""Enrichment step for private-sale listings.

Builds a ``DescribeInput`` from a ``PrivateListing``, calls the provider's
``describe()`` method (the pure LLM library — no auction-specific coupling),
and applies the ``EnrichmentOutput`` back to the listing row.

Mirrors the auction enricher's ``_build_describe_input`` / ``_apply_to_lot``
logic, with these private-sale differences:
  - ``auction_subtype="private"``
  - ``auctioneer_name=None``
  - ``current_high_bid_cad=listing.ask_price_cad``
  - ``bid_increment=None``
  - ``auction_close_at=None``
  - ``is_no_reserve=False``
  - ``image_count=len(listing.photos or [])``

AuctionLot-only columns (engine, transmission, drivetrain, condition_confidence,
condition_inferred_from_sparse_listing, description_quality, llm_concerns,
carfax_url, carfax_findings, mileage_is_verified, desirability_signals,
desirability_evidence, enrichment_version) are NOT set here — they don't exist
on ``PrivateListing``.
"""
from __future__ import annotations

from datetime import UTC, datetime

from carbuyer.db.enums import EnrichmentStatus, ValuationStatus
from carbuyer.db.models import PrivateListing
from carbuyer.llm.base import DescribeInput, DescribeProvider
from carbuyer.llm.carfax import find_carfax_url
from carbuyer.llm.schemas import EnrichmentOutput
from carbuyer.shared.logging import get_logger

log = get_logger("private_sale.enrich")

# Same sanity bounds as the auction enricher.
_MILEAGE_KM_MIN = 0
_MILEAGE_KM_MAX = 1_500_000
_YEAR_MIN = 1900


def _apply_enrichment(listing: PrivateListing, out: EnrichmentOutput) -> None:
    """Mutate the ORM row with enrichment output — pure CPU, no I/O.

    Only sets columns that exist on ``PrivateListing``. AuctionLot-only
    fields are intentionally omitted (see module docstring).
    """
    nv = out.normalized_vehicle
    year_cap = datetime.now(UTC).year + 1

    if nv.year is not None and not (_YEAR_MIN <= nv.year <= year_cap):
        log.warning(
            "rejecting out-of-range year from LLM",
            listing_id=listing.id,
            llm_year=nv.year,
        )
    else:
        listing.year = nv.year or listing.year

    listing.make = nv.make or listing.make
    listing.model = nv.model or listing.model
    listing.trim = nv.trim or listing.trim

    if nv.mileage_km is not None and not (
        _MILEAGE_KM_MIN <= nv.mileage_km <= _MILEAGE_KM_MAX
    ):
        log.warning(
            "rejecting out-of-range mileage from LLM",
            listing_id=listing.id,
            llm_mileage_km=nv.mileage_km,
        )
    else:
        listing.mileage_km = nv.mileage_km or listing.mileage_km

    listing.vin = nv.vin or listing.vin

    if out.title_status != "UNKNOWN":
        listing.title_status = out.title_status

    listing.condition_categorical = out.condition_categorical

    listing.red_flags = [f.model_dump() for f in out.red_flags]
    listing.green_flags = [f.model_dump() for f in out.green_flags]
    listing.showstopper_flags = [f.model_dump() for f in out.showstopper_flags]

    listing.summary = out.summary
    listing.desirable_trim_or_spec = out.rarity.desirable_trim_or_spec
    listing.classic_or_collector = out.rarity.classic_or_collector


async def enrich_private_listing(
    listing: PrivateListing,
    *,
    provider: DescribeProvider,
) -> None:
    """Enrich *listing* in-place using the given LLM provider.

    The caller is responsible for the surrounding DB transaction and for
    persisting the mutated row. This function is intentionally side-effect-free
    w.r.t. the DB — it only mutates the ORM object.
    """
    payload = DescribeInput(
        lot_id=listing.id,
        title=listing.title or "",
        description=listing.description or "",
        year=listing.year,
        make=listing.make,
        model=listing.model,
        auctioneer_name=None,
        auction_subtype="private",
        pickup_province=listing.pickup_province,
        raw_carfax_url=find_carfax_url(listing.description or ""),
        current_high_bid_cad=listing.ask_price_cad,
        bid_increment=None,
        auction_close_at=None,
        is_no_reserve=False,
        image_count=len(listing.photos or []),
        current_year=datetime.now(UTC).year,
    )
    out = await provider.describe(payload)
    _apply_enrichment(listing, out)
    listing.enrichment_status = EnrichmentStatus.DONE.value
    # Enrichment changed the vehicle facts -> the listing must be (re-)valued.
    listing.valuation_status = ValuationStatus.PENDING.value
