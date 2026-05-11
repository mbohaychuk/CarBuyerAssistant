"""Tests for the needs_plugin alert pipeline (Task 42).

Covers:
  - render_needs_plugin_text: happy path and all-None optional fields.
  - select_channel routing for "needs_plugin" trigger.
  - _process_needs_plugin: happy path, auction-not-found, already-stamped,
    non-unknown source, no channel configured, post failure (no stamp written).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import render_needs_plugin_text
from carbuyer.apps.notifier import notifier as notifier_mod
from carbuyer.apps.notifier.notifier import (  # pyright: ignore[reportPrivateUsage]
    _process_needs_plugin,
)
from carbuyer.db.models import Auction

# ─── render_needs_plugin_text ───


def test_render_needs_plugin_text_happy_path() -> None:
    start = datetime(2026, 8, 15, tzinfo=UTC)
    text = render_needs_plugin_text(
        auction_id=42,
        url="https://example.com/auction/42",
        auctioneer_name="Prairie Auctions",
        pickup_city="Lethbridge",
        pickup_province="AB",
        scheduled_start_at=start,
    )
    assert "NEW PLATFORM" in text
    assert "Prairie Auctions" in text
    assert "Lethbridge, AB" in text
    assert "Aug 15" in text
    assert "https://example.com/auction/42" in text
    assert "auction id 42" in text


def test_render_needs_plugin_text_all_none_optional_fields() -> None:
    text = render_needs_plugin_text(
        auction_id=7,
        url="https://example.com/auction/7",
        auctioneer_name=None,
        pickup_city=None,
        pickup_province=None,
        scheduled_start_at=None,
    )
    assert "(unknown)" in text
    assert "Location: ?" in text
    assert "(start date unknown)" in text
    assert "auction id 7" in text


def test_render_needs_plugin_text_city_only() -> None:
    text = render_needs_plugin_text(
        auction_id=1,
        url="https://x",
        auctioneer_name=None,
        pickup_city="Calgary",
        pickup_province=None,
        scheduled_start_at=None,
    )
    assert "Location: Calgary" in text


def test_render_needs_plugin_text_province_only() -> None:
    text = render_needs_plugin_text(
        auction_id=1,
        url="https://x",
        auctioneer_name=None,
        pickup_city=None,
        pickup_province="SK",
        scheduled_start_at=None,
    )
    assert "Location: SK" in text


# ─── channel routing ───


def test_select_channel_needs_plugin() -> None:
    assert select_channel(trigger="needs_plugin", score=None) == "needs_plugin"


# ─── _process_needs_plugin ───


def _seed_unknown_auction(session: AsyncSession) -> Auction:
    a = Auction(
        source="unknown:weirdplatform.com",
        source_auction_id="X1",
        url="https://weirdplatform.com/auction/X1",
        canonical_url="https://weirdplatform.com/auction/X1",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        pickup_city="Edmonton",
        pickup_province="AB",
        scheduled_start_at=datetime(2026, 9, 1, tzinfo=UTC),
        auctioneer_name="Weird Auctions Inc.",
    )
    session.add(a)
    return a


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

    monkeypatch.setattr(notifier_mod, "get_session", fake_get_session)
    return session


@pytest.mark.asyncio
async def test_process_needs_plugin_happy_path(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    auction = _seed_unknown_auction(session)
    await session.flush()

    posted_args: list[tuple[int, str]] = []

    async def fake_post_simple(
        channel_id: int,
        content: str,
        *,
        session: object = None,
    ) -> bool:
        posted_args.append((channel_id, content))
        return True

    monkeypatch.setattr(notifier_mod, "post_simple_message", fake_post_simple)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"needs_plugin": 55555},
    )

    http = MagicMock()
    await _process_needs_plugin(auction.id, http_session=http)

    assert len(posted_args) == 1
    assert posted_args[0][0] == 55555  # noqa: PLR2004
    assert "weirdplatform.com" in posted_args[0][1] or "Weird Auctions" in posted_args[0][1]

    await session.refresh(auction)
    assert auction.needs_plugin_notified_at is not None


@pytest.mark.asyncio
async def test_process_needs_plugin_auction_not_found(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-existent auction_id returns early without crashing."""
    post_calls: list[object] = []

    async def fake_post_simple(
        channel_id: int,
        content: str,
        *,
        session: object = None,
    ) -> bool:
        post_calls.append(channel_id)
        return True

    monkeypatch.setattr(notifier_mod, "post_simple_message", fake_post_simple)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"needs_plugin": 55555},
    )

    http = MagicMock()
    await _process_needs_plugin(999_999, http_session=http)

    assert not post_calls


@pytest.mark.asyncio
async def test_process_needs_plugin_already_stamped_no_repost(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If needs_plugin_notified_at is already set, no post is made."""
    session = _patched_get_session
    auction = _seed_unknown_auction(session)
    auction.needs_plugin_notified_at = datetime.now(UTC)
    await session.flush()

    post_calls: list[object] = []

    async def fake_post_simple(
        channel_id: int,
        content: str,
        *,
        session: object = None,
    ) -> bool:
        post_calls.append(channel_id)
        return True

    monkeypatch.setattr(notifier_mod, "post_simple_message", fake_post_simple)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"needs_plugin": 55555},
    )

    http = MagicMock()
    await _process_needs_plugin(auction.id, http_session=http)

    assert not post_calls


@pytest.mark.asyncio
async def test_process_needs_plugin_non_unknown_source_no_post(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source that doesn't start with 'unknown:' is silently skipped."""
    session = _patched_get_session
    auction = Auction(
        source="hibid",
        source_auction_id="H1",
        url="https://hibid.com/auction/H1",
        canonical_url="https://hibid.com/auction/H1",
        auction_subtype="estate",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session.add(auction)
    await session.flush()

    post_calls: list[object] = []

    async def fake_post_simple(
        channel_id: int,
        content: str,
        *,
        session: object = None,
    ) -> bool:
        post_calls.append(channel_id)
        return True

    monkeypatch.setattr(notifier_mod, "post_simple_message", fake_post_simple)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"needs_plugin": 55555},
    )

    http = MagicMock()
    await _process_needs_plugin(auction.id, http_session=http)

    assert not post_calls


@pytest.mark.asyncio
async def test_process_needs_plugin_no_channel_configured(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When needs_plugin channel is absent from settings, no post is made."""
    session = _patched_get_session
    auction = _seed_unknown_auction(session)
    await session.flush()

    post_calls: list[object] = []

    async def fake_post_simple(
        channel_id: int,
        content: str,
        *,
        session: object = None,
    ) -> bool:
        post_calls.append(channel_id)
        return True

    monkeypatch.setattr(notifier_mod, "post_simple_message", fake_post_simple)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {},
    )

    http = MagicMock()
    await _process_needs_plugin(auction.id, http_session=http)

    assert not post_calls


@pytest.mark.asyncio
async def test_process_needs_plugin_post_failure_no_stamp(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When post_simple_message returns False, no timestamp is written."""
    session = _patched_get_session
    auction = _seed_unknown_auction(session)
    await session.flush()

    async def fake_post_simple(
        channel_id: int,
        content: str,
        *,
        session: object = None,
    ) -> bool:
        return False

    monkeypatch.setattr(notifier_mod, "post_simple_message", fake_post_simple)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"needs_plugin": 55555},
    )

    http = MagicMock()
    await _process_needs_plugin(auction.id, http_session=http)

    await session.refresh(auction)
    assert auction.needs_plugin_notified_at is None
