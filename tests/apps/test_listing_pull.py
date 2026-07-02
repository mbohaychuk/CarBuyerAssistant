"""Want-list PULL ingestion + disappearance reconciliation. Exercised with a
fake ListingSource (no network)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.ingester import ingester as ingester_mod
from carbuyer.db.enums import ListingStatus
from carbuyer.db.models import PrivateListing, Search, VehicleOffer
from carbuyer.db.upserts import upsert_private_listing
from carbuyer.scoring.comps import build_comp_set
from carbuyer.sources.base import SOURCES, ListingRef, ListingSource, RawListing
from carbuyer.wants.criteria import WantCriteria


class _FakeListingSource(ListingSource):
    name = "fake"
    version = "0.1"

    def __init__(self, listings: list[RawListing]) -> None:
        self.listings = listings  # mutable so a later run can drop a listing
        self.searches = 0

    async def search_listings(
        self, criteria: WantCriteria,
    ) -> AsyncIterator[RawListing]:
        self.searches += 1
        for raw in self.listings:
            yield raw


def _raw(
    source_listing_id: str, make: str = "Nissan", *, source: str = "fake",
) -> RawListing:
    return RawListing(
        ref=ListingRef(source=source, source_listing_id=source_listing_id, url=f"http://f/{source_listing_id}"),
        title=f"{make} thing", description="x" * 50,
        make=make, model="Xterra", year=2010, mileage_km=150000,
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


# ─── PULL ───


async def test_pull_source_dedups_across_wants(_patched_session: AsyncSession) -> None:
    session = _patched_session
    src = _FakeListingSource([_raw("K1"), _raw("K2")])
    # Two want criteria; the fake yields the same two listings for each. The
    # seen-set dedups, so each listing is ingested once, not once per want.
    crits = [WantCriteria(makes=["Nissan"]), WantCriteria(makes=["Toyota"])]

    seen = await ingester_mod._pull_source(src, crits)  # pyright: ignore[reportPrivateUsage]

    assert src.searches == 2  # noqa: PLR2004 -- queried once per criteria
    assert seen == {"K1", "K2"}
    session.expire_all()
    assert await _count(session, PrivateListing) == 2  # noqa: PLR2004 -- two listings
    assert await _count(session, VehicleOffer) == 2  # noqa: PLR2004 -- two parents


async def test_pull_source_idempotent_on_rerun(_patched_session: AsyncSession) -> None:
    session = _patched_session
    src = _FakeListingSource([_raw("K1")])
    crits = [WantCriteria(makes=["Nissan"])]

    await ingester_mod._pull_source(src, crits)  # pyright: ignore[reportPrivateUsage]
    await ingester_mod._pull_source(src, crits)  # pyright: ignore[reportPrivateUsage]

    session.expire_all()
    assert await _count(session, PrivateListing) == 1  # second run updates, no dup


# ─── disappearance reconciliation ───


async def test_reconcile_marks_unseen_active_listings_removed(
    _patched_session: AsyncSession,
) -> None:
    session = _patched_session
    await upsert_private_listing(session, _raw("K1"), parser_version="0.1")
    await upsert_private_listing(session, _raw("K2"), parser_version="0.1")
    await session.commit()

    # This run saw only K1 → K2 dropped out → removed.
    removed = await ingester_mod._reconcile_disappeared("fake", {"K1"})  # pyright: ignore[reportPrivateUsage]
    assert removed == 1

    session.expire_all()
    rows = {
        pl.source_listing_id: pl
        for pl in (await session.execute(select(PrivateListing))).scalars().all()
    }
    assert rows["K1"].listing_status == ListingStatus.ACTIVE.value
    assert rows["K2"].listing_status == ListingStatus.REMOVED.value
    assert rows["K2"].disappeared_at is not None


async def test_reconcile_skips_when_run_saw_nothing(
    _patched_session: AsyncSession,
) -> None:
    session = _patched_session
    await upsert_private_listing(session, _raw("K1"), parser_version="0.1")
    await session.commit()

    # Empty seen-set (e.g. a transient empty result) must NOT mass-remove.
    removed = await ingester_mod._reconcile_disappeared("fake", set())  # pyright: ignore[reportPrivateUsage]
    assert removed == 0
    session.expire_all()
    pl = (await session.execute(select(PrivateListing))).scalar_one()
    assert pl.listing_status == ListingStatus.ACTIVE.value


async def test_reconcile_scoped_to_source(_patched_session: AsyncSession) -> None:
    session = _patched_session
    await upsert_private_listing(session, _raw("K1", source="fake"), parser_version="0.1")
    await upsert_private_listing(session, _raw("O1", source="other"), parser_version="0.1")
    await session.commit()

    # Reconciling 'fake' (saw nothing of K1) must not touch the 'other' source.
    removed = await ingester_mod._reconcile_disappeared("fake", {"Zzz"})  # pyright: ignore[reportPrivateUsage]
    assert removed == 1
    session.expire_all()
    rows = {
        pl.source: pl.listing_status
        for pl in (await session.execute(select(PrivateListing))).scalars().all()
    }
    assert rows["fake"] == ListingStatus.REMOVED.value
    assert rows["other"] == ListingStatus.ACTIVE.value


async def test_run_listing_pull_reconciles_disappeared_across_runs(
    _patched_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_session
    session.add(Search(name="xterra", config={"makes": ["Nissan"]}))
    await session.commit()
    src = _FakeListingSource([_raw("K1"), _raw("K2")])
    monkeypatch.setattr(ingester_mod, "SOURCES", {"fake": src})

    await ingester_mod._run_listing_pull()  # pyright: ignore[reportPrivateUsage]  -- run 1: both present
    src.listings = [_raw("K1")]  # K2 sells / is delisted
    await ingester_mod._run_listing_pull()  # pyright: ignore[reportPrivateUsage]  -- run 2: only K1

    session.expire_all()
    rows = {
        pl.source_listing_id: pl.listing_status
        for pl in (await session.execute(select(PrivateListing))).scalars().all()
    }
    assert rows["K1"] == ListingStatus.ACTIVE.value
    assert rows["K2"] == ListingStatus.REMOVED.value


async def test_disappeared_listing_becomes_a_private_comp(
    _patched_session: AsyncSession,
) -> None:
    """The producer→consumer tie: once reconciled to removed (disappeared_at
    stamped), a listing feeds scoring.comps as a private-channel comp."""
    session = _patched_session
    await upsert_private_listing(session, _raw("K1"), parser_version="0.1")
    await session.commit()
    await ingester_mod._reconcile_disappeared("fake", {"Zzz"})  # pyright: ignore[reportPrivateUsage]

    comps = await build_comp_set(
        session, make="Nissan", model="Xterra", trim=None, year=2010, mileage_km=150000,
    )
    private = [c for c in comps if c.source == "private_listing"]
    assert len(private) == 1
    assert private[0].sale_channel == "private"
    assert private[0].price_cad == Decimal("8000")


# ─── strategy wiring ───


async def test_run_listing_pull_noop_without_wants(_patched_session: AsyncSession) -> None:
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


def test_ingester_import_registers_the_real_listing_sources() -> None:
    # Importing the ingester (done above) must register the concrete listing
    # sources, so _run_listing_pull's SOURCES.values() filter actually finds them.
    _ = ingester_mod  # the import is the point
    registered = {name for name, s in SOURCES.items() if isinstance(s, ListingSource)}
    assert {"kijiji", "craigslist"} <= registered
