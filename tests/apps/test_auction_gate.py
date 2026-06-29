"""WG2 — want-gated auction ingestion: a lot matching no active want is dropped
before any DB write / enrichment. Exercised with a fake auction source (no
network), mirroring test_listing_pull's harness."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.ingester import ingester as ingester_mod
from carbuyer.apps.ingester.ingester import _ingest_one_hibid_province, _load_active_criteria
from carbuyer.db.models import AuctionLot, Search
from carbuyer.sources.base import AuctionRef, LotRef, RawAuction, RawLot
from carbuyer.wants.criteria import WantCriteria

_Pair = tuple[RawAuction, RawLot]


class _FakeAuctionSource:
    def __init__(self, pairs: list[_Pair]) -> None:
        self.pairs = pairs
        self.calls = 0

    async def discover_vehicle_lots(self, province: str) -> AsyncIterator[_Pair]:
        self.calls += 1
        for pair in self.pairs:
            yield pair


def _auction() -> RawAuction:
    return RawAuction(
        ref=AuctionRef(source="test", source_auction_id="A1", url="https://x/a/1"),
        title="t", description=None, auctioneer_name="A Co", auctioneer_external_id="ac1",
        scheduled_start_at=None, scheduled_end_at=None, pickup_address=None,
        pickup_city=None, pickup_province="AB", pickup_window_text=None,
        buyer_premium_pct=Decimal("0.10"), online_bidding_fee_pct=None, terms_text=None,
    )


def _lot(
    sid: str, *, make: str | None, model: str | None, title: str | None, year: int = 2010,
) -> RawLot:
    return RawLot(
        ref=LotRef(source="test", source_auction_id="A1", source_lot_id=sid, url=f"https://x/lot/{sid}"),
        lot_number=sid, title=title, description="runs and drives",
        photos=["https://x/p.jpg"], year=year, make=make, model=model,
        current_high_bid_cad=Decimal("2500"), scheduled_end_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


@pytest.fixture
def _patched_session(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(ingester_mod, "get_session", fake_get_session)
    return session


async def _makes(session: AsyncSession) -> list[str]:
    return [m for (m,) in (await session.execute(select(AuctionLot.make))).all()]


def _src(pairs: list[_Pair]) -> Any:
    return _FakeAuctionSource(pairs)


async def test_keeps_only_wanted_lots(_patched_session: AsyncSession) -> None:
    crit = [WantCriteria(makes=["Nissan"], models=["Xterra"])]
    src = _src([
        (_auction(), _lot("L1", make="Nissan", model="Xterra", title="2010 Nissan Xterra")),
        (_auction(), _lot("L2", make="Honda", model="Civic", title="2015 Honda Civic")),
    ])
    n = await _ingest_one_hibid_province(src, "AB", crit)
    assert n == 1
    assert await _makes(_patched_session) == ["Nissan"]


async def test_matches_via_title_when_make_unparsed(_patched_session: AsyncSession) -> None:
    # The scraper didn't structure make/model, but the title carries them.
    crit = [WantCriteria(makes=["Nissan"], models=["Xterra"])]
    src = _src([(_auction(), _lot("L1", make=None, model=None, title="2010 Nissan Xterra PRO-4X"))])
    n = await _ingest_one_hibid_province(src, "AB", crit)
    assert n == 1
    assert await _makes(_patched_session) == [None]  # make written raw (None); enricher fills it


async def test_no_wants_ingests_nothing(_patched_session: AsyncSession) -> None:
    src = _src([(_auction(), _lot("L1", make="Nissan", model="Xterra", title="Nissan Xterra"))])
    n = await _ingest_one_hibid_province(src, "AB", [])  # no active wants
    assert n == 0
    assert await _makes(_patched_session) == []


async def test_load_active_criteria_reads_enabled_wants(_patched_session: AsyncSession) -> None:
    _patched_session.add(Search(
        name="w", config=WantCriteria(makes=["Nissan"]).model_dump(mode="json"),
    ))
    await _patched_session.commit()
    crit = await _load_active_criteria()
    assert [c.makes for c in crit] == [["Nissan"]]


async def test_load_active_criteria_empty_without_wants(_patched_session: AsyncSession) -> None:
    assert await _load_active_criteria() == []
