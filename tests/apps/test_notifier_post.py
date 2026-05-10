"""Tests for discord_post.py — direct REST POST to Discord.

Uses unittest.mock.AsyncMock to mock aiohttp.ClientSession.post without
needing aioresponses (not in the project's dev dependencies).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from carbuyer.apps.notifier.discord_post import post_message


def _mock_response(status: int, headers: dict[str, str] | None = None) -> MagicMock:
    """Build a mock aiohttp response context-manager for a single call."""
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.text = AsyncMock(return_value="error body")
    return resp


def _make_session(*responses: MagicMock) -> MagicMock:
    """Build a mock aiohttp.ClientSession whose .post() returns responses in order."""
    session = MagicMock()
    call_count = [0]

    @asynccontextmanager  # type: ignore[arg-type]
    async def _post(*args: Any, **kwargs: Any) -> Any:
        idx = call_count[0]
        call_count[0] += 1
        yield responses[idx]

    session.post = _post
    return session


def _make_session_with_errors(*side_effects: MagicMock | Exception) -> MagicMock:
    """Build a session where each .post() call either yields a response or raises."""
    session = MagicMock()
    call_count = [0]

    @asynccontextmanager  # type: ignore[arg-type]
    async def _post(*args: Any, **kwargs: Any) -> Any:
        idx = call_count[0]
        call_count[0] += 1
        effect = side_effects[idx]
        if isinstance(effect, BaseException):
            raise effect
        yield effect

    session.post = _post
    return session


# ─── post_message ───


@pytest.mark.asyncio
async def test_post_message_no_token_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.settings.discord_bot_token", "",
    )
    # Pass a session mock; if token check fails before HTTP, no call is made.
    session = MagicMock()
    result = await post_message(12345, "hello", 1, session=session)
    assert result is False
    # post was never called
    assert not session.post.called


@pytest.mark.asyncio
async def test_post_message_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.settings.discord_bot_token", "tok123",
    )
    captured: list[dict[str, Any]] = []

    session = MagicMock()

    @asynccontextmanager  # type: ignore[arg-type]
    async def _post(url: str, *, headers: dict[str, str], json: Any) -> Any:
        captured.append({"url": url, "headers": headers, "json": json})
        resp = MagicMock()
        resp.status = 200
        resp.headers = {}
        yield resp

    session.post = _post

    result = await post_message(99, "test content", 7, session=session)
    assert result is True
    assert len(captured) == 1
    call = captured[0]
    assert "channels/99/messages" in call["url"]
    assert call["headers"]["Authorization"] == "Bot tok123"
    # Verify action-row component shape: one row, three buttons with correct custom_ids.
    comps = call["json"]["components"]
    assert len(comps) == 1
    row = comps[0]
    assert row["type"] == 1  # ACTION_ROW
    buttons = row["components"]
    assert len(buttons) == 3  # noqa: PLR2004
    custom_ids = [b["custom_id"] for b in buttons]
    assert "deal:interested:7" in custom_ids
    assert "deal:maybe:7" in custom_ids
    assert "deal:not_interested:7" in custom_ids
    styles = [b["style"] for b in buttons]
    assert 3 in styles  # noqa: PLR2004  # SUCCESS (green)
    assert 2 in styles  # noqa: PLR2004  # SECONDARY (gray)
    assert 4 in styles  # noqa: PLR2004  # DANGER (red)


@pytest.mark.asyncio
async def test_post_message_429_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """First call returns 429; second succeeds. Sleep is skipped by patching."""
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.settings.discord_bot_token", "tok",
    )
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.asyncio.sleep", AsyncMock(),
    )

    r1 = _mock_response(429, {"Retry-After": "0.01"})
    r2 = _mock_response(200)
    session = _make_session(r1, r2)

    result = await post_message(1, "msg", 5, session=session)
    assert result is True


@pytest.mark.asyncio
async def test_post_message_429_twice_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.settings.discord_bot_token", "tok",
    )
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.asyncio.sleep", AsyncMock(),
    )

    r1 = _mock_response(429, {"Retry-After": "0.01"})
    r2 = _mock_response(429, {"Retry-After": "0.01"})
    session = _make_session(r1, r2)

    result = await post_message(1, "msg", 5, session=session)
    assert result is False


@pytest.mark.asyncio
async def test_post_message_400_returns_false_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.settings.discord_bot_token", "tok",
    )

    call_count = [0]
    session = MagicMock()

    @asynccontextmanager  # type: ignore[arg-type]
    async def _post(*args: Any, **kwargs: Any) -> Any:
        call_count[0] += 1
        resp = MagicMock()
        resp.status = 400
        resp.headers = {}
        resp.text = AsyncMock(return_value="bad request")
        yield resp

    session.post = _post

    result = await post_message(1, "msg", 5, session=session)
    assert result is False
    # 400 is not retried
    assert call_count[0] == 1


@pytest.mark.asyncio
async def test_post_message_network_error_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First POST raises ClientError; second succeeds. Returns True after 2 attempts."""
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.settings.discord_bot_token", "tok",
    )
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.asyncio.sleep", AsyncMock(),
    )

    r2 = _mock_response(200)
    session = _make_session_with_errors(aiohttp.ClientError("conn reset"), r2)

    result = await post_message(1, "msg", 5, session=session)
    assert result is True


@pytest.mark.asyncio
async def test_post_message_network_error_twice_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both POSTs raise ClientError. Returns False after exactly 2 attempts."""
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.settings.discord_bot_token", "tok",
    )
    monkeypatch.setattr(
        "carbuyer.apps.notifier.discord_post.asyncio.sleep", AsyncMock(),
    )

    session = _make_session_with_errors(
        aiohttp.ClientError("conn reset"),
        aiohttp.ClientError("conn reset again"),
    )

    result = await post_message(1, "msg", 5, session=session)
    assert result is False
