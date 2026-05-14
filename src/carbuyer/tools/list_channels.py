"""Print the Discord channels visible to the bot in DISCORD_GUILD_ID.

Usage:
    python -m carbuyer.tools.list_channels

Reads DISCORD_BOT_TOKEN and DISCORD_GUILD_ID from .env (or the environment)
and queries GET /guilds/{guild_id}/channels. Prints a name → id mapping so
operators can verify channel naming or copy IDs into DISCORD_CHANNELS.

This helper does NOT modify anything. It's safe to run repeatedly.
"""
from __future__ import annotations

import asyncio
import sys

from carbuyer.apps.notifier.channel_resolver import _fetch_guild_channels  # pyright: ignore[reportPrivateUsage]
from carbuyer.shared.config import settings


async def main() -> None:
    if not settings.discord_bot_token:
        sys.exit("DISCORD_BOT_TOKEN not configured")
    if settings.discord_guild_id is None:
        sys.exit("DISCORD_GUILD_ID not configured")

    name_to_id = await _fetch_guild_channels(
        settings.discord_guild_id, settings.discord_bot_token,
    )
    if not name_to_id:
        print("(guild has no channels visible to this bot — has it been invited?)")
        return

    width = max(len(n) for n in name_to_id)
    for name in sorted(name_to_id):
        print(f"  {name.ljust(width)}  {name_to_id[name]}")


if __name__ == "__main__":
    asyncio.run(main())
