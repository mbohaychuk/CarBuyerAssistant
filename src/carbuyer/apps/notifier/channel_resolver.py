"""Discord channel name → ID resolution.

DISCORD_CHANNELS may contain either integer channel IDs or string channel
names. Names are operator-friendly (immutable in config even when channels
are recreated, readable in `.env`) but Discord's REST API only addresses
channels by ID. This module resolves any name values once at notifier
startup via GET /guilds/{guild_id}/channels and returns an all-ID map for
the rest of the pipeline to consume.

A "#hot-deals" prefix is tolerated because operators copy that form from
Discord routinely. Channel names not present in the guild are dropped with
a warning; the notifier already handles missing channels gracefully so
this matches the existing failure mode.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

import aiohttp

from carbuyer.shared.logging import get_logger

log = get_logger("channel_resolver")

_DISCORD_API = "https://discord.com/api/v10"


def _is_channel_id(value: object) -> bool:
    """A bare integer or all-digit string is treated as a channel ID.

    Discord normalizes channel names to lowercase with non-letters replaced
    by hyphens — a name cannot consist entirely of digits, so this heuristic
    avoids ambiguity without an extra config knob.
    """
    if isinstance(value, int):
        return True
    if isinstance(value, str) and value.isdigit():
        return True
    return False


async def _fetch_guild_channels(
    guild_id: int, bot_token: str,
) -> dict[str, int]:
    """Discord REST: GET /guilds/{guild_id}/channels → {name: id}."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{_DISCORD_API}/guilds/{guild_id}/channels",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=aiohttp.ClientTimeout(total=10.0),
        ) as resp:
            resp.raise_for_status()
            channels = await resp.json()
    return {str(c["name"]): int(c["id"]) for c in channels}


async def resolve_channels(
    raw: Mapping[str, int | str],
    *,
    guild_id: int | None,
    bot_token: str,
    _fetcher: Callable[
        [int, str], Awaitable[dict[str, int]],
    ] = _fetch_guild_channels,
) -> dict[str, int]:
    """Resolve a {key: name-or-id} map to {key: id}.

    Fast path: if every value is already an ID, return without calling
    Discord. Otherwise fetch the guild's channels once and resolve all name
    entries against that snapshot. Names not found are dropped from the
    output with a warning.

    Raises ValueError if any value is a name but guild_id is None — without
    a guild scope there's no way to resolve.
    """
    if not raw:
        return {}

    if all(_is_channel_id(v) for v in raw.values()):
        return {k: int(v) for k, v in raw.items()}

    if guild_id is None:
        raise ValueError(
            "DISCORD_GUILD_ID must be set when DISCORD_CHANNELS uses names",
        )

    name_to_id = await _fetcher(guild_id, bot_token)
    log.info(
        "discord guild channel snapshot",
        guild_id=guild_id,
        channel_count=len(name_to_id),
    )

    resolved: dict[str, int] = {}
    for key, value in raw.items():
        if _is_channel_id(value):
            resolved[key] = int(value)
            continue
        name = str(value).lstrip("#")
        ch_id = name_to_id.get(name)
        if ch_id is None:
            log.warning(
                "channel name not found in guild; key disabled",
                channel_key=key,
                channel_name=name,
                guild_id=guild_id,
            )
            continue
        resolved[key] = ch_id
        log.info(
            "channel resolved",
            channel_key=key,
            channel_name=name,
            channel_id=ch_id,
        )

    return resolved
