from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.auction_digest import runner as runner_mod
from carbuyer.apps.auction_digest.runner import run_digests
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch

# starts_at in (NOW, NOW+24h] qualifies as "within 24h"
NOW = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)
_FAKE_CHANNEL_ID = 4242


def _auction(session: AsyncSession, *, sid: str, starts_at: datetime | None,
             status: str = "upcoming", digest_sent_at: datetime | None = None) -> Auction:
    a = Auction(
        source="t", source_auction_id=sid, url=f"https://x/{sid}", canonical_url=f"https://x/{sid}",
        auction_subtype="estate", first_seen_at=NOW, last_seen_at=NOW,
        scheduled_start_at=starts_at, status=status, digest_sent_at=digest_sent_at,
        title=f"Auction {sid}", pickup_city="Headingley", pickup_province="MB",
    )
    session.add(a)
    return a


def _lot(session: AsyncSession, a: Auction, *, sid: str, **ov: object) -> AuctionLot:
    lot = AuctionLot(auction=a, source_lot_id=sid, url=f"https://x/{sid}",
                     title=f"Car {sid}", make="Ford", model="Mustang", year=1968,
                     lot_status="open", **ov)
    session.add(lot)
    return lot


@pytest.fixture
def _patched(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(runner_mod, "get_session", fake_get_session)
    # Force a known channel id so no Discord resolution/network happens.
    monkeypatch.setattr(runner_mod, "_resolve_digest_channel", _fake_resolve)
    return session


async def _fake_resolve() -> int:
    return _FAKE_CHANNEL_ID


@pytest.mark.asyncio
async def test_eligible_auction_with_matches_posts_and_marks(
    _patched: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched
    posts: list[tuple[int, str]] = []

    async def fake_post(channel_id: int, content: str, *, session: object = None) -> bool:
        posts.append((channel_id, content))
        return True

    monkeypatch.setattr(runner_mod, "post_simple_message", fake_post)

    a = _auction(session, sid="A1", starts_at=NOW + timedelta(hours=10))
    await session.flush()
    lot = _lot(session, a, sid="L1")
    s = SavedSearch(name="60s Mustang", make="Ford")
    session.add(s)
    await session.flush()
    session.add(SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=lot.id))
    await session.flush()

    await run_digests(now=NOW)

    assert len(posts) == 1
    assert posts[0][0] == _FAKE_CHANNEL_ID
    assert "Car L1" in posts[0][1]
    await session.refresh(a)
    assert a.digest_sent_at is not None


@pytest.mark.asyncio
async def test_empty_composition_marks_sent_without_posting(
    _patched: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched
    posts: list[object] = []

    async def fake_post(channel_id: int, content: str, *, session: object = None) -> bool:
        posts.append(content)
        return True

    monkeypatch.setattr(runner_mod, "post_simple_message", fake_post)
    a = _auction(session, sid="A1", starts_at=NOW + timedelta(hours=10))  # no lots/matches
    await session.flush()

    await run_digests(now=NOW)

    assert posts == []          # nothing to send
    await session.refresh(a)
    assert a.digest_sent_at is not None  # but marked, so it won't re-evaluate


@pytest.mark.asyncio
async def test_already_sent_and_out_of_window_skipped(
    _patched: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched
    posts: list[object] = []

    async def fake_post(channel_id: int, content: str, *, session: object = None) -> bool:
        posts.append(content)
        return True

    monkeypatch.setattr(runner_mod, "post_simple_message", fake_post)
    _auction(session, sid="SENT", starts_at=NOW + timedelta(hours=5), digest_sent_at=NOW)
    _auction(session, sid="FAR", starts_at=NOW + timedelta(days=3))      # >24h out
    _auction(session, sid="PAST", starts_at=NOW - timedelta(hours=1))    # already started
    _auction(session, sid="CANCELLED", starts_at=NOW + timedelta(hours=5), status="cancelled")
    await session.flush()

    await run_digests(now=NOW)
    assert posts == []  # none eligible
