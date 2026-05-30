"""Tests for upsert_private_listing: insert + status-cascade semantics."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import EnrichmentStatus, ValuationStatus
from carbuyer.db.private_upsert import upsert_private_listing
from carbuyer.sources.base import RawPrivateListing
from carbuyer.sources.resolver import canonicalize_url


def _raw(**overrides: object) -> RawPrivateListing:
    base: dict[str, object] = dict(
        source="kijiji",
        source_listing_id="12345",
        url="https://www.kijiji.ca/v-cars-trucks/city/2015-toyota-tacoma/12345",
        title="2015 Toyota Tacoma",
        description="Clean truck, no rust.",
        ask_price_cad=Decimal("22000"),
        pickup_province="AB",
        pickup_city="Calgary",
        year=2015,
        make="Toyota",
        model="Tacoma",
    )
    base.update(overrides)
    return RawPrivateListing(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fresh_insert_pending(session: AsyncSession) -> None:
    """Fresh upsert creates a PrivateListing with enrichment/valuation pending."""
    raw = _raw()
    listing = await upsert_private_listing(session, raw)
    await session.flush()

    assert listing.source == "kijiji"
    assert listing.source_listing_id == "12345"
    assert listing.canonical_url == canonicalize_url(raw.url)
    assert listing.enrichment_status == EnrichmentStatus.PENDING
    assert listing.valuation_status == ValuationStatus.PENDING
    assert listing.ask_price_cad == Decimal("22000")
    assert listing.title == "2015 Toyota Tacoma"


@pytest.mark.asyncio
async def test_content_change_resets_statuses(session: AsyncSession) -> None:
    """Re-upsert with changed ask_price_cad or title resets statuses to pending
    and bumps last_seen_at."""
    raw = _raw()
    listing = await upsert_private_listing(session, raw)
    await session.flush()

    # Simulate downstream workers advancing status.
    listing.enrichment_status = EnrichmentStatus.DONE
    listing.valuation_status = ValuationStatus.DONE
    await session.flush()

    first_seen = listing.last_seen_at

    # Re-upsert with changed price + title.
    raw2 = _raw(ask_price_cad=Decimal("19500"), title="2015 Toyota Tacoma TRD")
    listing2 = await upsert_private_listing(session, raw2)
    await session.flush()

    assert listing2.ask_price_cad == Decimal("19500")
    assert listing2.title == "2015 Toyota Tacoma TRD"
    assert listing2.enrichment_status == EnrichmentStatus.PENDING
    assert listing2.valuation_status == ValuationStatus.PENDING
    # last_seen_at should be at least as recent as the first insert.
    assert listing2.last_seen_at >= first_seen


@pytest.mark.asyncio
async def test_unchanged_upsert_preserves_statuses(session: AsyncSession) -> None:
    """Idempotent re-upsert with identical content does NOT reset statuses."""
    raw = _raw()
    listing = await upsert_private_listing(session, raw)
    await session.flush()

    listing.enrichment_status = EnrichmentStatus.DONE
    listing.valuation_status = ValuationStatus.DONE
    await session.flush()

    # Re-upsert with the exact same data.
    raw2 = _raw()
    listing2 = await upsert_private_listing(session, raw2)
    await session.flush()

    assert listing2.enrichment_status == EnrichmentStatus.DONE
    assert listing2.valuation_status == ValuationStatus.DONE
