"""Tests for the stale-source watchdog.

The watchdog reads auctions.last_seen_at per source, compares against
STALE_THRESHOLD, applies dedup via source_alert_state, and posts to the
configured system_health Discord channel. These tests pin each of those
behaviors in isolation via direct calls to _check_and_alert with a fake
Discord poster and a controlled SOURCES registry.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.source_watchdog import watchdog as watchdog_mod
from carbuyer.apps.source_watchdog.watchdog import (
    ALERT_DEDUP_WINDOW,
    STALE_THRESHOLD,
    _check_and_alert,
    _format_alert,
)
from carbuyer.db.models import Auction, SourceAlertState


def _seed_auction(
    session: AsyncSession, *, source: str, last_seen_at: datetime,
) -> Auction:
    a = Auction(
        source=source,
        source_auction_id=f"{source}-A1",
        url=f"https://{source}.example/a/1",
        canonical_url=f"https://{source}.example/a/1",
        auction_subtype="estate",
        first_seen_at=last_seen_at,
        last_seen_at=last_seen_at,
    )
    session.add(a)
    return a


@pytest.fixture
def _patched_session(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(watchdog_mod, "get_session", fake_get_session)
    return session


@pytest.fixture
def _patched_sources(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace SOURCES with a controlled two-source registry."""
    fake_sources: dict[str, object] = {"hibid": object(), "mcdougall": object()}
    monkeypatch.setattr(watchdog_mod, "SOURCES", fake_sources)
    return fake_sources


@pytest.fixture
def _fake_poster(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[int, str]]:
    """Capture Discord posts; return the list of (channel_id, content)."""
    posts: list[tuple[int, str]] = []

    async def fake_post(
        channel_id: int, content: str, *, session: Any = None,
    ) -> bool:
        posts.append((channel_id, content))
        return True

    monkeypatch.setattr(watchdog_mod, "post_simple_message", fake_post)
    return posts


# ── stale detection ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_source_does_not_alert(
    _patched_session: AsyncSession,
    _patched_sources: dict[str, object],
    _fake_poster: list[tuple[int, str]],
) -> None:
    now = datetime.now(UTC)
    _seed_auction(
        _patched_session, source="hibid",
        last_seen_at=now - timedelta(hours=1),
    )
    _seed_auction(
        _patched_session, source="mcdougall",
        last_seen_at=now - timedelta(hours=2),
    )
    await _patched_session.commit()

    posted = await _check_and_alert(http_session=None, channel_id=999)  # type: ignore[arg-type]

    assert posted == 0
    assert _fake_poster == []


@pytest.mark.asyncio
async def test_stale_source_alerts(
    _patched_session: AsyncSession,
    _patched_sources: dict[str, object],
    _fake_poster: list[tuple[int, str]],
) -> None:
    """A source that hasn't ingested in >STALE_THRESHOLD posts to Discord
    and records the alert state row so the next run dedups."""
    now = datetime.now(UTC)
    stale_last_seen = now - STALE_THRESHOLD - timedelta(hours=2)
    _seed_auction(
        _patched_session, source="hibid", last_seen_at=stale_last_seen,
    )
    _seed_auction(
        _patched_session, source="mcdougall",
        last_seen_at=now - timedelta(hours=1),  # fresh
    )
    await _patched_session.commit()

    posted = await _check_and_alert(http_session=None, channel_id=42)  # type: ignore[arg-type]

    assert posted == 1
    assert len(_fake_poster) == 1
    channel_id, content = _fake_poster[0]
    assert channel_id == 42
    assert "hibid" in content
    # And the alert state was persisted so the next run dedups.
    row = await _patched_session.get(SourceAlertState, "hibid")
    assert row is not None
    # Loaded as timezone-aware UTC (the column is TIMESTAMPTZ).
    assert row.last_alerted_at.tzinfo is not None


@pytest.mark.asyncio
async def test_never_ingested_source_does_not_alert(
    _patched_session: AsyncSession,
    _patched_sources: dict[str, object],
    _fake_poster: list[tuple[int, str]],
) -> None:
    """A registered source with zero auction rows is a brand-new plugin;
    don't alert (the operator knows it's new). Once it ingests anything,
    subsequent dark periods will be alerted on."""
    # mcdougall has activity; hibid has none.
    now = datetime.now(UTC)
    _seed_auction(
        _patched_session, source="mcdougall",
        last_seen_at=now - timedelta(hours=1),
    )
    await _patched_session.commit()

    posted = await _check_and_alert(http_session=None, channel_id=42)  # type: ignore[arg-type]

    assert posted == 0
    assert _fake_poster == []


# ── dedup ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_source_within_dedup_window_does_not_realert(
    _patched_session: AsyncSession,
    _patched_sources: dict[str, object],
    _fake_poster: list[tuple[int, str]],
) -> None:
    """Hourly runs against a persistently-stale source must not re-alert
    within ALERT_DEDUP_WINDOW. Without this guard, a 24h-stale source
    generates 24 Discord pings/day."""
    now = datetime.now(UTC)
    _seed_auction(
        _patched_session, source="hibid",
        last_seen_at=now - STALE_THRESHOLD - timedelta(hours=10),
    )
    # Pre-seed alert state showing we alerted 1h ago.
    recent_alert = SourceAlertState(
        source="hibid", last_alerted_at=now - timedelta(hours=1),
    )
    _patched_session.add(recent_alert)
    await _patched_session.commit()

    posted = await _check_and_alert(http_session=None, channel_id=42)  # type: ignore[arg-type]

    assert posted == 0
    assert _fake_poster == []


@pytest.mark.asyncio
async def test_stale_source_past_dedup_window_realerts(
    _patched_session: AsyncSession,
    _patched_sources: dict[str, object],
    _fake_poster: list[tuple[int, str]],
) -> None:
    """Once ALERT_DEDUP_WINDOW elapses, a still-stale source re-alerts.
    Required so a long-running outage produces visible pings every ~24h
    rather than going silent after the first alert."""
    now = datetime.now(UTC)
    _seed_auction(
        _patched_session, source="hibid",
        last_seen_at=now - STALE_THRESHOLD - timedelta(days=3),
    )
    old_alert = SourceAlertState(
        source="hibid",
        last_alerted_at=now - ALERT_DEDUP_WINDOW - timedelta(hours=2),
    )
    _patched_session.add(old_alert)
    await _patched_session.commit()

    posted = await _check_and_alert(http_session=None, channel_id=42)  # type: ignore[arg-type]

    assert posted == 1
    assert len(_fake_poster) == 1
    # Alert state was upserted to now (or close enough).
    await _patched_session.refresh(old_alert)
    assert now - old_alert.last_alerted_at < timedelta(seconds=5)


# ── alert formatting ────────────────────────────────────────────────────────


def test_format_alert_includes_source_and_age() -> None:
    now = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
    last_seen = now - timedelta(hours=30)
    msg = _format_alert("mcdougall", last_seen, now)
    assert "mcdougall" in msg
    assert "30h" in msg
    assert last_seen.isoformat() in msg


# ── poster failure path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_post_does_not_record_alert_state(
    _patched_session: AsyncSession,
    _patched_sources: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Discord post fails (token missing, rate-limited, etc.), the
    alert state must NOT be recorded — otherwise the dedup window starts
    and the operator never sees the alert. Next run will retry."""
    now = datetime.now(UTC)
    _seed_auction(
        _patched_session, source="hibid",
        last_seen_at=now - STALE_THRESHOLD - timedelta(hours=2),
    )
    await _patched_session.commit()

    async def failing_post(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(watchdog_mod, "post_simple_message", failing_post)

    posted = await _check_and_alert(http_session=None, channel_id=42)  # type: ignore[arg-type]

    assert posted == 0
    row = await _patched_session.get(SourceAlertState, "hibid")
    assert row is None  # not recorded, will retry next run
