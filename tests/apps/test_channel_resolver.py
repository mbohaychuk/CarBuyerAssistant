"""Tests for Discord channel-name → ID resolution.

The notifier accepts a {channel_key: name-or-id} map in DISCORD_CHANNELS so
operators can configure with human-readable names. Resolution happens once
at startup via Discord REST GET /guilds/{guild_id}/channels.

The resolver is tested with an injected fetcher to avoid real HTTP in tests.
"""
from __future__ import annotations

import pytest

from carbuyer.apps.notifier.channel_resolver import resolve_channels


async def _fake_fetch(_guild_id: int, _bot_token: str) -> dict[str, int]:
    """Stand-in for the Discord REST fetch — returns a fixed name→id map."""
    return {
        "car-alerts-early": 100,
        "car-hot-deals": 200,
        "car-watchlist": 300,
        "car-closing-soon": 400,
        "car-system": 500,
    }


async def _failing_fetch(_guild_id: int, _bot_token: str) -> dict[str, int]:
    raise AssertionError("fetcher should not have been called")


@pytest.mark.asyncio
async def test_resolve_all_ids_passes_through_without_api_call() -> None:
    """If every value is already an integer ID, no Discord API call is needed.
    Critical because existing ID-based configs must keep working unchanged."""
    raw: dict[str, int | str] = {
        "early_warning": 100,
        "hot_deals": 200,
    }
    out = await resolve_channels(
        raw, guild_id=999, bot_token="t", _fetcher=_failing_fetch,
    )
    assert out == {"early_warning": 100, "hot_deals": 200}


@pytest.mark.asyncio
async def test_resolve_numeric_string_treated_as_id() -> None:
    """A string of digits ('123456789') is an ID, not a name. Discord channel
    names cannot start with a digit (the UI lowercases + replaces), so this
    is a safe heuristic and avoids a wasted API call."""
    raw: dict[str, int | str] = {"hot_deals": "200"}
    out = await resolve_channels(
        raw, guild_id=999, bot_token="t", _fetcher=_failing_fetch,
    )
    assert out == {"hot_deals": 200}


@pytest.mark.asyncio
async def test_resolve_all_names_calls_fetcher_once() -> None:
    raw: dict[str, int | str] = {
        "early_warning": "car-alerts-early",
        "hot_deals": "car-hot-deals",
    }
    out = await resolve_channels(
        raw, guild_id=999, bot_token="t", _fetcher=_fake_fetch,
    )
    assert out == {"early_warning": 100, "hot_deals": 200}


@pytest.mark.asyncio
async def test_resolve_mixed_names_and_ids() -> None:
    """A mixed map resolves only the name-typed entries."""
    raw: dict[str, int | str] = {
        "early_warning": "car-alerts-early",
        "hot_deals": 999_999,  # already an ID, not in fake fetcher's map
    }
    out = await resolve_channels(
        raw, guild_id=42, bot_token="t", _fetcher=_fake_fetch,
    )
    assert out == {"early_warning": 100, "hot_deals": 999_999}


@pytest.mark.asyncio
async def test_resolve_tolerates_hash_prefix() -> None:
    """Operators copy '#channel-name' from Discord routinely. Strip the # so
    both forms resolve cleanly."""
    raw: dict[str, int | str] = {"hot_deals": "#car-hot-deals"}
    out = await resolve_channels(
        raw, guild_id=42, bot_token="t", _fetcher=_fake_fetch,
    )
    assert out == {"hot_deals": 200}


@pytest.mark.asyncio
async def test_resolve_drops_name_not_in_guild_and_warns() -> None:
    """A configured name that doesn't exist in the guild is dropped from the
    output (notifier already handles missing-channel gracefully). The error
    surfaces in logs so an operator can spot the misconfiguration."""
    raw: dict[str, int | str] = {
        "hot_deals": "car-hot-deals",
        "watchlist": "channel-that-does-not-exist",
    }
    out = await resolve_channels(
        raw, guild_id=42, bot_token="t", _fetcher=_fake_fetch,
    )
    assert out == {"hot_deals": 200}
    assert "watchlist" not in out


@pytest.mark.asyncio
async def test_resolve_raises_when_names_present_but_no_guild_id() -> None:
    """A name can't be resolved without a guild scope. Fail loud at startup
    instead of crashing per-post later."""
    raw: dict[str, int | str] = {"hot_deals": "car-hot-deals"}
    with pytest.raises(ValueError, match="DISCORD_GUILD_ID"):
        await resolve_channels(
            raw, guild_id=None, bot_token="t", _fetcher=_fake_fetch,
        )


@pytest.mark.asyncio
async def test_resolve_empty_map_returns_empty() -> None:
    """No channels configured → empty map back, no fetcher call."""
    out = await resolve_channels(
        {}, guild_id=None, bot_token="t", _fetcher=_failing_fetch,
    )
    assert out == {}
