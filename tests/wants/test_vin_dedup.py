"""Cross-source VIN dedup: the same vehicle (VIN) listed on several sources is
separate offer rows, so it could fire a want-alert per source. upsert_want_match
auto-dismisses the duplicate so the vehicle alerts once."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.notifier.notifier import _load_want_alerts
from carbuyer.apps.valuator.valuator import _has_unnotified_instant_match
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, Search, WantMatch
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria
from carbuyer.wants.service import backfill_want

_VIN = "1N6AD0EV5AC400001"


async def _auction(session: AsyncSession) -> Auction:
    a = Auction(
        source="hibid", source_auction_id="A1", url="x", canonical_url="x",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()
    return a


def _auction_lot(
    auction: Auction, *, vin: str | None, sid: str = "L1", lot_status: str = "open",
) -> AuctionLot:
    return AuctionLot(
        auction_id=auction.id, source_lot_id=sid, url=f"http://x/{sid}",
        make="Nissan", model="Xterra", year=2010, vin=vin, lot_status=lot_status,
        current_high_bid_cad=Decimal("8000"),
    )


def _private(*, vin: str | None, sid: str = "K1") -> PrivateListing:
    return PrivateListing(
        source="kijiji", source_listing_id=sid, url=f"http://k/{sid}",
        make="Nissan", model="Xterra", year=2010, vin=vin,
        asking_price_cad=Decimal("8000"), listing_status="active",
    )


async def _want(session: AsyncSession, name: str = "x") -> int:
    want = await repo.create_want(session, name=name, criteria=WantCriteria(makes=["Nissan"]))
    await session.flush()
    return want.id


async def _match(session: AsyncSession, want_id: int, offer_id: int) -> WantMatch:
    m, _ = await repo.upsert_want_match(
        session, search_id=want_id, lot_id=offer_id, want_relative_score=0.2,
    )
    return m


async def test_auto_dismisses_duplicate_vin_across_sources(session: AsyncSession) -> None:
    want_id = await _want(session)
    auction = await _auction(session)
    lot = _auction_lot(auction, vin=_VIN)
    listing = _private(vin=_VIN)  # same VIN, different source
    session.add_all([lot, listing])
    await session.flush()

    m1 = await _match(session, want_id, lot.id)
    m2 = await _match(session, want_id, listing.id)

    assert m1.dismissed is False  # first offer for the VIN alerts
    assert m2.dismissed is True   # the cross-source duplicate is suppressed


async def test_different_vins_both_alert(session: AsyncSession) -> None:
    want_id = await _want(session)
    auction = await _auction(session)
    a = _auction_lot(auction, vin="VINAAAAAAAAAAAAA1", sid="L1")
    b = _auction_lot(auction, vin="VINBBBBBBBBBBBBB2", sid="L2")
    session.add_all([a, b])
    await session.flush()

    assert (await _match(session, want_id, a.id)).dismissed is False
    assert (await _match(session, want_id, b.id)).dismissed is False


async def test_null_vin_is_not_deduped(session: AsyncSession) -> None:
    want_id = await _want(session)
    auction = await _auction(session)
    a = _auction_lot(auction, vin=None, sid="L1")
    b = _auction_lot(auction, vin=None, sid="L2")
    session.add_all([a, b])
    await session.flush()

    await _match(session, want_id, a.id)
    assert (await _match(session, want_id, b.id)).dismissed is False  # no VIN → no dedup


async def test_dedup_is_case_insensitive(session: AsyncSession) -> None:
    want_id = await _want(session)
    auction = await _auction(session)
    a = _auction_lot(auction, vin=_VIN.lower(), sid="L1")
    b = _auction_lot(auction, vin=_VIN.upper(), sid="L2")
    session.add_all([a, b])
    await session.flush()

    await _match(session, want_id, a.id)
    assert (await _match(session, want_id, b.id)).dismissed is True


async def test_dismissed_sibling_does_not_suppress(session: AsyncSession) -> None:
    # If the user dismissed the first match, a same-VIN offer on another source is
    # still allowed to alert (the dismissal was of that listing, not the VIN).
    want_id = await _want(session)
    auction = await _auction(session)
    a = _auction_lot(auction, vin=_VIN, sid="L1")
    b = _private(vin=_VIN, sid="K1")
    session.add_all([a, b])
    await session.flush()

    m1 = await _match(session, want_id, a.id)
    m1.dismissed = True  # user dismissed it
    await session.flush()
    assert (await _match(session, want_id, b.id)).dismissed is False


async def test_dead_primary_does_not_suppress_live_duplicate(session: AsyncSession) -> None:
    # A sold/closed auction lot must NOT shadow a still-active relisting of the
    # same VIN on another source (the auction->retail flip).
    want_id = await _want(session)
    auction = await _auction(session)
    sold = _auction_lot(auction, vin=_VIN, sid="L1", lot_status="sold")  # dead primary
    relist = _private(vin=_VIN, sid="K1")  # live relisting, different source
    session.add_all([sold, relist])
    await session.flush()

    await _match(session, want_id, sold.id)
    assert (await _match(session, want_id, relist.id)).dismissed is False


async def test_dedup_is_scoped_per_want(session: AsyncSession) -> None:
    # Same VIN, two different wants → each want's first match alerts.
    auction = await _auction(session)
    lot = _auction_lot(auction, vin=_VIN)
    session.add(lot)
    w1 = await _want(session, "w1")
    w2 = await _want(session, "w2")

    assert (await _match(session, w1, lot.id)).dismissed is False
    assert (await _match(session, w2, lot.id)).dismissed is False  # different want


async def test_dismissed_duplicate_does_not_alert_on_either_path(session: AsyncSession) -> None:
    # The dedup only WORKS because both alerting paths filter dismissed: the
    # valuator's force-PENDING (_has_unnotified_instant_match) and the notifier's
    # want-alert builder (_load_want_alerts). Pin that tie. (score 0.2 → instant.)
    want_id = await _want(session)
    auction = await _auction(session)
    primary = _auction_lot(auction, vin=_VIN, sid="L1")  # live
    dup = _private(vin=_VIN, sid="K1")  # live cross-source duplicate
    session.add_all([primary, dup])
    await session.flush()

    await _match(session, want_id, primary.id)
    assert (await _match(session, want_id, dup.id)).dismissed is True

    _now = datetime.now(UTC)
    assert await _has_unnotified_instant_match(
        session, primary, scheduled_end_at=None, now=_now,
    ) is True   # primary alerts
    assert await _has_unnotified_instant_match(
        session, dup, scheduled_end_at=None, now=_now,
    ) is False  # dup does not
    assert await _load_want_alerts(
        session, dup, pickup_province=None, scheduled_end_at=None, now=_now,
    ) == []  # nor in the notifier


async def test_reeval_of_dismissed_duplicate_stays_dismissed(session: AsyncSession) -> None:
    # Re-valuation re-upserts the existing match (score-only); the dedup is not
    # re-run and dismissed must not flap back on.
    want_id = await _want(session)
    auction = await _auction(session)
    primary = _auction_lot(auction, vin=_VIN, sid="L1")
    dup = _private(vin=_VIN, sid="K1")
    session.add_all([primary, dup])
    await session.flush()

    await _match(session, want_id, primary.id)
    assert (await _match(session, want_id, dup.id)).dismissed is True
    m, created = await repo.upsert_want_match(
        session, search_id=want_id, lot_id=dup.id, want_relative_score=0.9,
    )
    assert created is False
    assert m.dismissed is True  # stays suppressed across re-evaluation


async def test_backfill_dedups_same_vin(session: AsyncSession) -> None:
    # backfill_want goes through the same upsert_want_match, so it dedups too, and
    # its returned count excludes the auto-dismissed duplicate.
    auction = await _auction(session)
    lot = _auction_lot(auction, vin=_VIN, sid="L1")  # auction processed first → primary
    listing = _private(vin=_VIN, sid="K1")
    lot.valuation_status = "done"
    listing.valuation_status = "done"
    session.add_all([lot, listing])
    want = await repo.create_want(session, name="bf", criteria=WantCriteria(makes=["Nissan"]))
    await session.flush()

    n = await backfill_want(session, want)
    assert n == 1  # two same-VIN offers → one findable match

    rows = (
        await session.execute(select(WantMatch).where(WantMatch.search_id == want.id))
    ).scalars().all()
    assert {m.lot_id for m in rows} == {lot.id, listing.id}  # both match rows exist
    assert sum(1 for m in rows if not m.dismissed) == 1  # exactly one is live


async def test_has_unnotified_instant_match_ignores_disabled_search(session: AsyncSession) -> None:
    """FIX 6: a disabled Search with an un-notified match must not force PENDING."""
    auction = await _auction(session)
    lot = _auction_lot(auction, vin=None, sid="L-disabled")
    session.add(lot)
    want = Search(name="disabled-want", config={}, enabled=False)
    session.add(want)
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=lot.id, want_relative_score=0.5)
    session.add(wm)
    await session.flush()

    result = await _has_unnotified_instant_match(
        session, lot, scheduled_end_at=None, now=datetime.now(UTC),
    )
    assert result is False
