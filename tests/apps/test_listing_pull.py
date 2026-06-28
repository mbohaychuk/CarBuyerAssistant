"""S5 — want-list PULL ingestion: query listing sources per want criteria and
upsert deduped private listings. Exercised with a fake ListingSource."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.ingester import ingester as ingester_mod
from carbuyer.db.models import PrivateListing, Search, VehicleOffer
from carbuyer.sources.base import ListingRef, ListingSource, RawListing
from carbuyer.wants.criteria import WantCriteria


class _FakeListingSource(ListingSource):
    name = "fake"
    version = "0.1"

    def __init__(self, listings: list[RawListing]) -> None:
        self._listings = listings
        self.searches = 0

    async def search_listings(
        self, criteria: WantCriteria,
    ) -> AsyncIterator[RawListing]:
        self.searches += 1
        for raw in self._listings:
            yield raw


def _raw(source_listing_id: str, make: str = "Nissan") -> RawListing:
    return RawListing(
        ref=ListingRef(source="fake", source_listing_id=source_listing_id, url=f"http://f/{source_listing_id}"),
        title=f"{make} thing", description="x" * 50,
        make=make, model="Xterra", year=2010,
        asking_price_cad=Decimal("8000"), seller_type="private",
        location_province="AB", listing_status="active",
    )


@pytest.fixture
def _patched_session(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(ingester_mod, "get_session", fake_get_session)
    return session


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_pull_listings_dedups_across_wants(_patched_session: AsyncSession) -> None:
    session = _patched_session
    src = _FakeListingSource([_raw("K1"), _raw("K2")])
    # Two want criteria; the fake yields the same two listings for each. Dedup
    # means each listing is ingested exactly once, not once per matching want.
    crits = [WantCriteria(makes=["Nissan"]), WantCriteria(makes=["Toyota"])]

    n = await ingester_mod._pull_listings([src], crits)  # pyright: ignore[reportPrivateUsage]

    assert src.searches == 2  # noqa: PLR2004 -- queried once per criteria
    assert n == 2  # noqa: PLR2004 -- K1, K2 upserted once each despite 2 criteria
    session.expire_all()
    assert await _count(session, PrivateListing) == 2  # noqa: PLR2004 -- two listings
    assert await _count(session, VehicleOffer) == 2  # noqa: PLR2004 -- two parents


async def test_pull_listings_idempotent_on_rerun(_patched_session: AsyncSession) -> None:
    session = _patched_session
    src = _FakeListingSource([_raw("K1")])
    crits = [WantCriteria(makes=["Nissan"])]

    await ingester_mod._pull_listings([src], crits)  # pyright: ignore[reportPrivateUsage]
    await ingester_mod._pull_listings([src], crits)  # pyright: ignore[reportPrivateUsage]

    session.expire_all()
    assert await _count(session, PrivateListing) == 1  # second run updates, no dup


async def test_run_listing_pull_noop_without_wants(_patched_session: AsyncSession) -> None:
    # No enabled wants → strategy is a no-op even though a source is registered.
    n = await ingester_mod._run_listing_pull()  # pyright: ignore[reportPrivateUsage]
    assert n == 0


async def test_run_listing_pull_reads_enabled_wants(
    _patched_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_session
    session.add(Search(name="xterra", config={"makes": ["Nissan"]}))
    await session.commit()
    src = _FakeListingSource([_raw("K1")])
    monkeypatch.setattr(ingester_mod, "SOURCES", {"fake": src})

    n = await ingester_mod._run_listing_pull()  # pyright: ignore[reportPrivateUsage]

    assert n == 1
    session.expire_all()
    assert await _count(session, PrivateListing) == 1
