"""Tests for notifier.py — worker logic against the test DB.

Uses the same _patched_get_session pattern as test_enricher.py: patches
get_session and get_session_maker on the notifier module so fresh sessions
share the test's outer rolled-back transaction.

After the WG5 flipper teardown the only auction triggers are the watched-lot
auction-timing reminders (closing_soon, lot_extended); want-match alerts are
the primary notification path.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.notifier import notifier as notifier_mod
from carbuyer.apps.notifier.notifier import (  # pyright: ignore[reportPrivateUsage]
    _process_one,
    process_pending,
)
from carbuyer.db.enums import NotificationStatus
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, Search, WantMatch


def _seed_lot(
    session: AsyncSession,
    *,
    price_deal_score: float | None = None,
    confidence_bucket: str | None = "high",
    user_action: str | None = None,
    scheduled_end_at: datetime | None = None,
    lot_status: str = "open",
    closing_notified_at: datetime | None = None,
    notification_status: str = "pending",
) -> tuple[Auction, AuctionLot]:
    end = scheduled_end_at or datetime(2026, 6, 10, tzinfo=UTC)
    a = Auction(
        source="test",
        source_auction_id="A1",
        url="https://x",
        canonical_url="https://x",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        scheduled_end_at=end,
        pickup_province="AB",
    )
    session.add(a)
    lot = AuctionLot(
        auction=a,
        source_lot_id="L1",
        url="https://x/lot/1",
        title="2015 Toyota Land Cruiser",
        description="x" * 200,
        price_deal_score=price_deal_score,
        confidence_bucket=confidence_bucket,
        user_action=user_action,
        lot_status=lot_status,
        closing_notified_at=closing_notified_at,
        showstopper_flags=[],
        notification_status=notification_status,
        # Minimal valuator output so _embed_data doesn't crash.
        all_in_at_current_bid_cad=None,
        expected_value_cad=None,
        value_low_cad=None,
        value_high_cad=None,
    )
    session.add(lot)
    return a, lot


def _seed_closing_soon_lot(
    session: AsyncSession, *, notification_status: str = "in_progress",
) -> tuple[Auction, AuctionLot]:
    """A watched lot closing within the hour — fires the closing_soon trigger."""
    return _seed_lot(
        session,
        user_action="interested",
        scheduled_end_at=datetime.now(UTC) + timedelta(minutes=30),
        lot_status="closing_soon",
        notification_status=notification_status,
    )


@pytest.fixture
def _patched_get_session(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    """Patch notifier's get_session and get_session_maker to use the test
    connection so uncommitted savepoint data is visible within the test.
    """
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    def fake_get_session_maker() -> object:
        return maker

    monkeypatch.setattr(notifier_mod, "get_session", fake_get_session)
    monkeypatch.setattr(notifier_mod, "get_session_maker", fake_get_session_maker)
    return session


# ─── want-match notifications ───


@pytest.mark.asyncio
async def test_process_one_posts_want_match_and_stamps_ledger(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An un-notified want_match posts to the 'wants' channel + stamps the ledger."""
    session = _patched_get_session
    _, lot = _seed_lot(session, price_deal_score=0.0)
    lot.make, lot.model, lot.year = "Nissan", "Xterra", 2010
    lot.expected_value_cad = Decimal("10000")
    lot.value_mid_cad = Decimal("10000")
    lot.comp_count = 9
    lot.current_high_bid_cad = Decimal("8000")
    want = Search(name="manual xterra", config={})
    session.add(want)
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=lot.id, want_relative_score=0.2)
    session.add(wm)
    await session.flush()
    wm_id, lot_id = wm.id, lot.id

    posted: list[tuple[int, str, int]] = []

    async def fake_post(
        channel_id: int, content: str, lid: int, *, session: object = None
    ) -> bool:
        posted.append((channel_id, content, lid))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242}
    )

    outcome = await _process_one(lot_id, http_session=MagicMock())
    assert outcome == "done"
    assert len(posted) == 1
    assert posted[0][0] == 4242  # noqa: PLR2004 -- the configured wants channel id
    assert "manual xterra" in posted[0][1]

    session.expire_all()
    refreshed = await session.get(WantMatch, wm_id)
    assert refreshed is not None
    assert refreshed.notified_at is not None


@pytest.mark.asyncio
async def test_process_one_posts_want_match_for_private_listing(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private listing (no auction child) fires its want alert and does not
    crash on the absent auction; auction triggers simply don't apply."""
    session = _patched_get_session
    listing = PrivateListing(
        source="kijiji", source_listing_id="K1", url="http://k/1",
        title="2010 Nissan Xterra", description="x" * 200,
        make="Nissan", model="Xterra", year=2010,
        asking_price_cad=Decimal("8000"), seller_type="private",
        location_province="AB", listing_status="active",
        expected_value_cad=Decimal("10000"), value_mid_cad=Decimal("10000"),
        comp_count=9, price_deal_score=0.2,
        notification_status="pending",
    )
    session.add(listing)
    want = Search(name="manual xterra", config={})
    session.add(want)
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=listing.id, want_relative_score=0.2)
    session.add(wm)
    await session.flush()
    wm_id, lid = wm.id, listing.id

    posted: list[tuple[int, str, int]] = []

    async def fake_post(
        channel_id: int, content: str, lid_: int, *, session: object = None
    ) -> bool:
        posted.append((channel_id, content, lid_))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242}
    )

    outcome = await _process_one(lid, http_session=MagicMock())
    assert outcome == "done"
    assert len(posted) == 1
    assert posted[0][0] == 4242  # noqa: PLR2004 -- the configured wants channel id
    assert "manual xterra" in posted[0][1]

    session.expire_all()
    refreshed = await session.get(WantMatch, wm_id)
    assert refreshed is not None
    assert refreshed.notified_at is not None


@pytest.mark.asyncio
async def test_process_one_renders_price_drop_for_private_listing(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private listing whose asking dropped (previous_asking > asking) and
    whose want_match was re-opened fires a want alert rendered as a price drop."""
    session = _patched_get_session
    listing = PrivateListing(
        source="kijiji", source_listing_id="K1", url="http://k/1",
        title="2005 Lexus GX 470", description="x" * 200,
        make="Lexus", model="GX 470", year=2005,
        asking_price_cad=Decimal("13500"), previous_asking_price_cad=Decimal("15000"),
        seller_type="private", location_province="AB", listing_status="active",
        expected_value_cad=Decimal("17000"), value_mid_cad=Decimal("17000"),
        comp_count=6, price_deal_score=0.2,
        notification_status="pending",
    )
    session.add(listing)
    want = Search(name="gx470 base", config={})
    session.add(want)
    await session.flush()
    # Un-notified (re-opened by the price-drop reset).
    session.add(WantMatch(search_id=want.id, lot_id=listing.id, want_relative_score=0.2))
    await session.flush()
    lid = listing.id

    posted: list[str] = []

    async def fake_post(
        channel_id: int, content: str, lid_: int, *, session: object = None
    ) -> bool:
        posted.append(content)
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242}
    )

    outcome = await _process_one(lid, http_session=MagicMock())
    assert outcome == "done"
    assert len(posted) == 1
    assert "Price drop" in posted[0]
    assert "$15,000" in posted[0]  # was
    assert "$13,500" in posted[0]  # now


@pytest.mark.asyncio
async def test_process_one_skips_already_notified_want_match(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A want_match already stamped notified_at is not re-posted (fire-once)."""
    session = _patched_get_session
    _, lot = _seed_lot(session, price_deal_score=0.0)
    want = Search(name="w", config={})
    session.add(want)
    await session.flush()
    wm = WantMatch(
        search_id=want.id, lot_id=lot.id,
        want_relative_score=0.2, notified_at=datetime.now(UTC),
    )
    session.add(wm)
    await session.flush()
    lot_id = lot.id

    posted: list[int] = []

    async def fake_post(
        channel_id: int, content: str, lid: int, *, session: object = None
    ) -> bool:
        posted.append(lid)
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242}
    )

    outcome = await _process_one(lot_id, http_session=MagicMock())
    assert outcome == "skipped"
    assert posted == []


@pytest.mark.asyncio
async def test_process_one_ignores_muted_want(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A muted (disabled) want must not fire its already-queued matches."""
    session = _patched_get_session
    _, lot = _seed_lot(session, price_deal_score=0.0)
    want = Search(name="muted", config={}, enabled=False)
    session.add(want)
    await session.flush()
    session.add(WantMatch(search_id=want.id, lot_id=lot.id, want_relative_score=0.2))
    await session.flush()
    lot_id = lot.id

    posted: list[int] = []

    async def fake_post(c: int, content: str, lid: int, *, session: object = None) -> bool:
        posted.append(lid)
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242}
    )

    outcome = await _process_one(lot_id, http_session=MagicMock())
    assert outcome == "skipped"
    assert posted == []


@pytest.mark.asyncio
async def test_process_one_keeps_pending_when_a_want_post_fails(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two wants match; one post succeeds, one fails. The failed want must stay
    un-stamped and the lot must return to PENDING so it retries (fire-once)."""
    session = _patched_get_session
    _, lot = _seed_lot(session, price_deal_score=0.0)
    lot.make, lot.model, lot.year = "Nissan", "Xterra", 2010
    lot.current_high_bid_cad = Decimal("8000")
    want_a = Search(name="want-a", config={})
    want_b = Search(name="want-b", config={})
    session.add_all([want_a, want_b])
    await session.flush()
    wm_a = WantMatch(search_id=want_a.id, lot_id=lot.id, want_relative_score=0.2)
    wm_b = WantMatch(search_id=want_b.id, lot_id=lot.id, want_relative_score=0.2)
    session.add_all([wm_a, wm_b])
    await session.flush()
    wm_a_id, wm_b_id, lot_id = wm_a.id, wm_b.id, lot.id

    async def fake_post(c: int, content: str, lid: int, *, session: object = None) -> bool:
        return "want-b" not in content  # want-b's post fails

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242}
    )

    outcome = await _process_one(lot_id, http_session=MagicMock())
    assert outcome == "transient"
    session.expire_all()
    assert (await session.get(WantMatch, wm_a_id)).notified_at is not None  # type: ignore[union-attr]
    assert (await session.get(WantMatch, wm_b_id)).notified_at is None  # type: ignore[union-attr]
    lot_row = await session.get(AuctionLot, lot_id)
    assert lot_row.notification_status == NotificationStatus.PENDING  # type: ignore[union-attr]


# ─── _process_one: auction triggers (closing_soon / lot_extended) ───


@pytest.mark.asyncio
async def test_process_one_no_triggers_marks_skipped(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lot with no want match and no auction-timing trigger → 'skipped'."""
    session = _patched_get_session
    _, lot = _seed_lot(session, price_deal_score=0.05, notification_status="in_progress")
    await session.flush()

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "skipped"
    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_process_one_fires_closing_soon(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watched lot closing within the hour → closing_soon fires → DONE, and the
    closing_notified_at stamp is written so it doesn't re-fire."""
    session = _patched_get_session
    _, lot = _seed_closing_soon_lot(session)
    await session.flush()

    posted_calls: list[tuple[int, str, int]] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted_calls.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"auction_closing": 1234},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "done"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.DONE
    assert lot.closing_notified_at is not None
    assert lot.last_notified_channel == "auction_closing"
    assert len(posted_calls) == 1
    assert posted_calls[0][2] == lot.id


@pytest.mark.asyncio
async def test_process_one_post_failure_keeps_pending_and_increments_attempts(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 13 C2: when every post_message returns False (Discord blip),
    status returns to PENDING with notification_attempts incremented. Locks
    in the fix for 'lot looked DONE in the DB but no Discord message landed'.
    """
    session = _patched_get_session
    _, lot = _seed_closing_soon_lot(session)
    await session.flush()

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return False

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"auction_closing": 1234},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "transient"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.PENDING
    assert lot.notification_attempts == 1
    assert lot.last_notification_error is not None
    assert lot.closing_notified_at is None  # no stamp on failure


@pytest.mark.asyncio
async def test_process_one_post_failure_flips_failed_at_max_attempts(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After settings.notification_max_attempts unsuccessful attempts, the
    lot stops re-queueing and lands FAILED for ops to investigate."""
    from carbuyer.shared.config import settings as cfg

    session = _patched_get_session
    _, lot = _seed_closing_soon_lot(session)
    # Simulate cfg.notification_max_attempts - 1 prior failed attempts.
    lot.notification_attempts = cfg.notification_max_attempts - 1
    await session.flush()

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return False

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"auction_closing": 1234},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "failed"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.FAILED
    assert lot.notification_attempts == cfg.notification_max_attempts


@pytest.mark.asyncio
async def test_process_one_unconfigured_channel_marks_skipped(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every trigger lands on a missing channel — ops config gap, retrying
    won't help. SKIPPED with last_notification_error recorded."""
    session = _patched_get_session
    _, lot = _seed_closing_soon_lot(session)
    await session.flush()

    posted_calls: list[object] = []

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        posted_calls.append((channel_id, content, lot_id))
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "skipped"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.SKIPPED
    assert lot.last_notification_error is not None
    assert not posted_calls
    # Phase 13 review fix #2: no-channel SKIPPED isn't a delivery failure, so
    # the retry counter must not be polluted. Otherwise if ops fixes the
    # channel config and re-queues the lot, it starts closer to FAILED.
    assert lot.notification_attempts == 0


@pytest.mark.asyncio
async def test_process_one_missing_lot_returns_missing(
    _patched_get_session: AsyncSession,
) -> None:
    """A lot_id that doesn't exist returns 'missing' without crashing."""
    http = MagicMock()
    outcome = await _process_one(999_999, http_session=http)
    assert outcome == "missing"


# ─── process_pending ───


@pytest.mark.asyncio
async def test_process_pending_claims_and_processes(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: pending lots → claim_pending_lots → _process_one each → DONE."""
    session = _patched_get_session

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"auction_closing": 1234},
    )

    soon = datetime.now(UTC) + timedelta(minutes=30)
    a = Auction(
        source="test", source_auction_id="A2", url="https://y",
        canonical_url="https://y", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        scheduled_end_at=soon,
        pickup_province="AB",
    )
    session.add(a)
    await session.flush()

    lots = [
        AuctionLot(
            auction_id=a.id, source_lot_id=f"LP{i}",
            url=f"https://y/lot/{i}", title=f"lot {i}",
            description="x" * 200,
            user_action="interested",
            lot_status="closing_soon",
            notification_status="pending",
        )
        for i in range(3)
    ]
    session.add_all(lots)
    await session.flush()

    http = MagicMock()
    count = await process_pending(http_session=http)
    assert count == 3  # noqa: PLR2004

    for lot in lots:
        await session.refresh(lot)
        assert lot.notification_status == NotificationStatus.DONE


# ─── Phase 13 review: missing-import + uncovered-path regression ────────────


def test_notifier_module_imports_runtime_names() -> None:
    """Both `notify` and `recover_orphans` are referenced inside async
    functions that earlier tests never reached. Python's lazy name resolution
    let them slip through. This module-level attr check is the cheapest gate
    against a recurrence — fails at test-discovery if either import is
    deleted in a future refactor.
    """
    from carbuyer.apps.notifier import notifier as nm  # noqa: PLC0415

    assert callable(nm.notify), "notifier.py must import `notify`"
    assert callable(nm.recover_orphans), "notifier.py must import `recover_orphans`"


@pytest.mark.asyncio
async def test_process_pending_self_notifies_on_transient_failure(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When at least one lot ends `transient`, process_pending must reach the
    self-NOTIFY branch without raising. Historically a NameError lurked there
    because tests only seeded successful posts. The mere absence of an
    exception here is the regression signal.
    """
    session = _patched_get_session
    _, lot = _seed_closing_soon_lot(session, notification_status="pending")
    await session.flush()

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return False  # every post fails → transient → self-NOTIFY branch

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"auction_closing": 1234},
    )

    http = MagicMock()
    count = await process_pending(http_session=http)
    assert count == 1

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.PENDING
    assert lot.notification_attempts == 1


@pytest.mark.asyncio
async def test_catchup_sweep_recovers_orphaned_in_progress(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker crash between SELECT FOR UPDATE SKIP LOCKED and the terminal
    status write leaves the row stuck IN_PROGRESS. _catchup_sweep must flip
    these back to PENDING at startup so they're claimable again. Historically
    `recover_orphans` was called without being imported — this exercises the
    call site so the NameError can't hide.
    """
    from carbuyer.apps.notifier.notifier import (  # noqa: PLC0415
        _catchup_sweep,
    )

    session = _patched_get_session
    _, lot = _seed_lot(
        session,
        price_deal_score=0.05,  # no triggers → SKIPPED post-recovery
        notification_status="in_progress",  # the orphaned state
    )
    await session.flush()

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"auction_closing": 1234},
    )

    http = MagicMock()
    await _catchup_sweep(http_session=http)

    await session.refresh(lot)
    # Recovery + downstream processing both ran; the lot is no longer stuck.
    assert lot.notification_status != NotificationStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_process_one_skips_stale_want_match_criteria_no_longer_satisfied(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A want_match that is un-notified but whose criteria the lot no longer
    satisfies (e.g. price dropped the want ceiling below the bid) must not re-fire.
    FIX 3: _load_want_alerts re-checks matches() before queuing an alert."""
    session = _patched_get_session
    _, lot = _seed_lot(session, price_deal_score=0.0)
    lot.current_high_bid_cad = Decimal("10000")  # above the 5000 ceiling below
    # Want whose price ceiling is below the lot's current bid.
    want = Search(name="stale-want", config={"price_ceiling_cad": 5000})
    session.add(want)
    await session.flush()
    session.add(WantMatch(search_id=want.id, lot_id=lot.id, want_relative_score=0.2))
    await session.flush()
    lot_id = lot.id

    posted: list[int] = []

    async def fake_post(
        channel_id: int, content: str, lid: int, *, session: object = None,
    ) -> bool:
        posted.append(lid)
        return True

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242}
    )

    outcome = await _process_one(lot_id, http_session=MagicMock())
    assert outcome == "skipped"
    assert posted == []


def test_in_quiet_hours_wraparound_window() -> None:
    from carbuyer.apps.notifier.notifier import (  # pyright: ignore[reportPrivateUsage]
        _in_quiet_hours,
    )
    base = datetime(2026, 5, 13, tzinfo=UTC)
    assert _in_quiet_hours(base.replace(hour=22), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=2), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=7), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=8), 22, 8) is False
    assert _in_quiet_hours(base.replace(hour=12), 22, 8) is False
    assert _in_quiet_hours(base.replace(hour=21), 22, 8) is False


def test_in_quiet_hours_non_wraparound() -> None:
    from carbuyer.apps.notifier.notifier import (  # pyright: ignore[reportPrivateUsage]
        _in_quiet_hours,
    )
    base = datetime(2026, 5, 13, tzinfo=UTC)
    assert _in_quiet_hours(base.replace(hour=9), 9, 17) is True
    assert _in_quiet_hours(base.replace(hour=16), 9, 17) is True
    assert _in_quiet_hours(base.replace(hour=17), 9, 17) is False
    assert _in_quiet_hours(base.replace(hour=8), 9, 17) is False
