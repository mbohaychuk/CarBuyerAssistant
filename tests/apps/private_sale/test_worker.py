"""Worker integration tests — fake source, mocked LLM + Discord.

All DB I/O runs against ``carbuyer_test`` inside the test's outer savepoint
transaction (rolled back on teardown). No network calls are made.

The ``_patched_get_session`` fixture makes the worker's ``get_session()`` calls
reuse the test connection, so per-listing transactions become nested savepoints
that are visible within the same test.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.private_sale import worker as worker_mod
from carbuyer.apps.private_sale.worker import run_cycle
from carbuyer.db.enums import UserAction
from carbuyer.db.models import HistoricalSale, PrivateListing, SavedSearch, SavedSearchMatch
from carbuyer.llm.schemas import (
    EnrichmentOutput,
    NormalizedVehicle,
    RarityAssessment,
)
from carbuyer.sources.base import RawPrivateListing

# ─── Constants ───────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
_CHANNEL_ID = 999_000

# A price well below expected value (~14k for these comps) to produce a
# positive deal score above the default 0.15 threshold.
_ASK_DEAL = Decimal("8000")
# A price at or above market value: won't trigger a deal alert on its own.
# The comp range is 10k-19k so expected ~= 14.5k. 18k puts deal score negative.
_ASK_FAIR = Decimal("18000")

_COMP_PRICES = [10_000, 11_000, 12_000, 13_000, 14_000,
                15_000, 16_000, 17_000, 18_000, 19_000]

_YEAR = 2015
_MILEAGE = 150_000


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _seed_comps(session: AsyncSession) -> None:
    for p in _COMP_PRICES:
        session.add(HistoricalSale(
            year=_YEAR, mileage_km=_MILEAGE,
            make="Toyota", model="Tacoma", trim=None,
            sale_channel="auction_estate", sale_platform="hibid",
            title_status="NORMAL", schema_version=1,
            final_listed_price_cad=Decimal(p),
            final_price_with_premium_cad=Decimal(p),
            buyer_premium_pct_at_sale=Decimal("0.10"),
            disposition_reason="sold",
        ))


def _raw(
    *,
    listing_id: str = "fake-001",
    ask: Decimal = _ASK_DEAL,
    province: str = "AB",
) -> RawPrivateListing:
    return RawPrivateListing(
        source="fake_source",
        source_listing_id=listing_id,
        url=f"https://fake.example.com/listing/{listing_id}",
        title=f"2015 Toyota Tacoma ({listing_id})",
        description="Well maintained, no accidents.",
        photos=["https://fake.example.com/photo1.jpg"],
        year=_YEAR,
        make="Toyota",
        model="Tacoma",
        trim=None,
        mileage_km=_MILEAGE,
        ask_price_cad=ask,
        pickup_province=province,
    )


def _canned_enrichment_output() -> EnrichmentOutput:
    return EnrichmentOutput(
        normalized_vehicle=NormalizedVehicle(
            year=_YEAR,
            make="Toyota",
            model="Tacoma",
            trim=None,
            engine=None,
            transmission="unknown",
            drivetrain="unknown",
            mileage_km=_MILEAGE,
            mileage_is_verified=None,
            vin=None,
        ),
        title_status="NORMAL",
        condition_categorical="decent",
        condition_confidence=0.8,
        red_flags=[],
        green_flags=[],
        showstopper_flags=[],
        concerns=[],
        carfax_url=None,
        summary="Well maintained truck.",
        description_quality="adequate",
        rarity=RarityAssessment(
            desirable_trim_or_spec=False,
            classic_or_collector=False,
            desirability_signals=[],
            desirability_evidence=[],
        ),
    )


class FakeSource:
    """In-memory source yielding a configurable list of RawPrivateListing."""

    def __init__(self, listings: list[RawPrivateListing]) -> None:
        self._listings = listings

    async def iter_search_results(
        self, *, provinces: tuple[str, ...] = (),
    ) -> AsyncGenerator[RawPrivateListing, None]:
        for raw in self._listings:
            yield raw

    async def fetch_listing_detail(
        self, raw: RawPrivateListing,
    ) -> RawPrivateListing:
        return raw


class FakeProvider:
    """LLM provider that returns a canned EnrichmentOutput."""

    def __init__(self, output: EnrichmentOutput | None = None) -> None:
        self._output = output or _canned_enrichment_output()
        self.calls: list[Any] = []

    async def describe(self, payload: Any) -> EnrichmentOutput:
        self.calls.append(payload)
        return self._output


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def _patched_get_session(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(worker_mod, "get_session", fake_get_session)
    return session


@pytest.fixture
def _patched_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve() -> int:
        return _CHANNEL_ID

    monkeypatch.setattr(worker_mod, "_resolve_private_channel", fake_resolve)


@pytest.fixture
def _posted_messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Captures content strings passed to post_simple_message; returns True."""
    captured: list[str] = []

    async def fake_post(channel_id: int, content: str, *, session: Any = None) -> bool:
        captured.append(content)
        return True

    monkeypatch.setattr(worker_mod, "post_simple_message", fake_post)
    return captured


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deal_listing_posts_and_stamps(
    _patched_get_session: AsyncSession,
    _patched_channel: None,
    _posted_messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listing priced well below market → deal alert fired + alerted_at stamped."""
    session = _patched_get_session
    _seed_comps(session)
    await session.flush()

    source = FakeSource([_raw(listing_id="deal-001", ask=_ASK_DEAL)])
    provider = FakeProvider()
    http = MagicMock()

    counts = await run_cycle(_NOW, source=source, provider=provider, http=http)

    assert len(_posted_messages) == 1
    assert "Toyota" in _posted_messages[0]
    assert counts["alerted"] == 1

    listing = (await session.execute(
        select(PrivateListing).where(
            PrivateListing.source == "fake_source",
            PrivateListing.source_listing_id == "deal-001",
        )
    )).scalar_one()
    assert listing.alerted_at == _NOW
    assert listing.last_alert_price_cad == _ASK_DEAL


@pytest.mark.asyncio
async def test_saved_search_match_posts_and_inserts_match_row(
    _patched_get_session: AsyncSession,
    _patched_channel: None,
    _posted_messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listing matching a SavedSearch (but not a deal) → alert + match row."""
    session = _patched_get_session
    _seed_comps(session)

    # A search that matches Toyota Tacoma, with a max_all_in_cost well above
    # the ask price (fair-priced ask so deal score is near 0).
    search = SavedSearch(
        name="Tacoma Watch",
        is_active=True,
        make="Toyota",
        model="Tacoma",
        max_all_in_cost_cad=20_000,
    )
    session.add(search)
    await session.flush()

    source = FakeSource([_raw(listing_id="match-001", ask=_ASK_FAIR)])
    provider = FakeProvider()
    http = MagicMock()

    counts = await run_cycle(_NOW, source=source, provider=provider, http=http)

    assert len(_posted_messages) == 1
    assert counts["alerted"] == 1

    match_row = (await session.execute(
        select(SavedSearchMatch).where(
            SavedSearchMatch.source_kind == "private_listing",
            SavedSearchMatch.saved_search_id == search.id,
        )
    )).scalar_one_or_none()
    assert match_row is not None


@pytest.mark.asyncio
async def test_no_deal_no_match_no_post(
    _patched_get_session: AsyncSession,
    _patched_channel: None,
    _posted_messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fair-priced listing with no matching searches → no Discord post."""
    session = _patched_get_session
    _seed_comps(session)
    await session.flush()

    # No SavedSearches in DB, ask at market value → no deal, no match.
    source = FakeSource([_raw(listing_id="fair-001", ask=_ASK_FAIR)])
    provider = FakeProvider()
    http = MagicMock()

    counts = await run_cycle(_NOW, source=source, provider=provider, http=http)

    assert len(_posted_messages) == 0
    assert counts["alerted"] == 0


@pytest.mark.asyncio
async def test_passed_listing_no_post(
    _patched_get_session: AsyncSession,
    _patched_channel: None,
    _posted_messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """user_action='passed' blocks alert even when it's a great deal."""
    session = _patched_get_session
    _seed_comps(session)
    await session.flush()

    source = FakeSource([_raw(listing_id="passed-001", ask=_ASK_DEAL)])
    provider = FakeProvider()
    http = MagicMock()

    # First cycle to upsert + enrich + value.
    await run_cycle(_NOW, source=source, provider=provider, http=http)

    # Simulate user marking the listing as passed.
    listing = (await session.execute(
        select(PrivateListing).where(
            PrivateListing.source == "fake_source",
            PrivateListing.source_listing_id == "passed-001",
        )
    )).scalar_one()
    listing.user_action = UserAction.PASSED
    # Clear alerted_at so the alert condition would fire if not for the pass guard.
    listing.alerted_at = None
    listing.last_alert_price_cad = None
    await session.flush()

    _posted_messages.clear()

    # Force a re-enrich + re-value by resetting statuses.
    listing.enrichment_status = "pending"
    listing.valuation_status = "pending"
    await session.flush()

    counts = await run_cycle(_NOW, source=source, provider=provider, http=http)

    assert len(_posted_messages) == 0
    assert counts["alerted"] == 0


@pytest.mark.asyncio
async def test_already_alerted_no_duplicate_post(
    _patched_get_session: AsyncSession,
    _patched_channel: None,
    _posted_messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listing that has already been alerted at the same price → no re-post."""
    session = _patched_get_session
    _seed_comps(session)
    await session.flush()

    source = FakeSource([_raw(listing_id="dup-001", ask=_ASK_DEAL)])
    provider = FakeProvider()
    http = MagicMock()

    # First run: should alert.
    await run_cycle(_NOW, source=source, provider=provider, http=http)
    assert len(_posted_messages) == 1

    _posted_messages.clear()

    # Second run with the same data: alerted_at is set, price unchanged → skip.
    counts = await run_cycle(_NOW, source=source, provider=provider, http=http)

    assert len(_posted_messages) == 0
    assert counts["alerted"] == 0


@pytest.mark.asyncio
async def test_price_drop_realerts(
    _patched_get_session: AsyncSession,
    _patched_channel: None,
    _posted_messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A price drop >= private_realert_drop_pct triggers a re-alert."""
    session = _patched_get_session
    _seed_comps(session)
    await session.flush()

    source = FakeSource([_raw(listing_id="drop-001", ask=_ASK_DEAL)])
    provider = FakeProvider()
    http = MagicMock()

    # First run → alerted at 8000.
    await run_cycle(_NOW, source=source, provider=provider, http=http)
    assert len(_posted_messages) == 1
    _posted_messages.clear()

    # Drop to 8000 * (1 - 0.10) - 1 = 7199 (> 10% below last alert price).
    from carbuyer.shared.config import settings  # noqa: PLC0415
    drop_threshold = _ASK_DEAL * (1 - Decimal(str(settings.private_realert_drop_pct)))
    dropped_ask = drop_threshold - Decimal("1")

    # Simulate the listing being re-scraped with a lower price.
    listing = (await session.execute(
        select(PrivateListing).where(
            PrivateListing.source == "fake_source",
            PrivateListing.source_listing_id == "drop-001",
        )
    )).scalar_one()
    listing.ask_price_cad = dropped_ask
    listing.enrichment_status = "pending"
    listing.valuation_status = "pending"
    await session.flush()

    source2 = FakeSource([_raw(listing_id="drop-001", ask=dropped_ask)])
    counts = await run_cycle(_NOW, source=source2, provider=provider, http=http)

    assert len(_posted_messages) == 1, f"Expected re-alert, got {len(_posted_messages)}"
    assert counts["alerted"] == 1

    # Verify the second-tx stamp persisted the new baseline.
    await session.refresh(listing)
    assert listing.last_alert_price_cad == dropped_ask
    assert listing.alerted_at == _NOW


@pytest.mark.asyncio
async def test_post_failure_leaves_alerted_at_null(
    _patched_get_session: AsyncSession,
    _patched_channel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the Discord POST fails, alerted_at stays NULL (retried next cycle)."""
    session = _patched_get_session
    _seed_comps(session)
    await session.flush()

    async def failing_post(
        channel_id: int, content: str, *, session: Any = None,
    ) -> bool:
        return False

    monkeypatch.setattr(worker_mod, "post_simple_message", failing_post)

    source = FakeSource([_raw(listing_id="fail-001", ask=_ASK_DEAL)])
    provider = FakeProvider()
    http = MagicMock()

    counts = await run_cycle(_NOW, source=source, provider=provider, http=http)

    assert counts["post_failed"] >= 1

    listing = (await session.execute(
        select(PrivateListing).where(
            PrivateListing.source == "fake_source",
            PrivateListing.source_listing_id == "fail-001",
        )
    )).scalar_one()
    assert listing.alerted_at is None


@pytest.mark.asyncio
async def test_per_listing_exception_isolation(
    _patched_get_session: AsyncSession,
    _patched_channel: None,
    _posted_messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception in one listing's enrich step does not prevent others from processing."""
    session = _patched_get_session
    _seed_comps(session)
    await session.flush()

    call_count = 0

    async def sometimes_explode(payload: Any) -> EnrichmentOutput:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated LLM failure")
        return _canned_enrichment_output()

    provider = FakeProvider()
    provider.describe = sometimes_explode  # type: ignore[method-assign]

    source = FakeSource([
        _raw(listing_id="explode-001", ask=_ASK_DEAL),
        _raw(listing_id="ok-002", ask=_ASK_DEAL),
    ])
    http = MagicMock()

    counts = await run_cycle(_NOW, source=source, provider=provider, http=http)

    assert counts["errors"] >= 1
    # The second listing should still be alerted.
    assert len(_posted_messages) >= 1
    assert any("ok-002" in m or "Toyota" in m for m in _posted_messages)
