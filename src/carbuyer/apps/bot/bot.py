"""discord.py runtime for the notifier bot.

The bot owns the connection to Discord; the Phase 6 notifier worker enqueues
messages via ``post_to_channel``. We register ``DynamicItem`` button classes
in ``setup_hook`` so persisted custom_ids dispatch correctly across restarts.

Intents are minimal — guilds only — because the bot does not read message
content, member lists, or presence. Application commands are synced lazily
(no slash commands defined yet, so ``tree.sync()`` is a no-op).
"""
from __future__ import annotations

import sys

import discord
from discord.ext import commands

from carbuyer.apps.bot.views import (
    LotInterestedButton,
    LotMaybeButton,
    LotNotInterestedButton,
)
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("bot")


def _intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


class CarbuyerBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=_intents())

    async def setup_hook(self) -> None:
        self.add_dynamic_items(
            LotInterestedButton, LotMaybeButton, LotNotInterestedButton,
        )
        if settings.discord_guild_id:
            guild = discord.Object(id=settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


async def post_to_channel(
    bot: CarbuyerBot,
    channel_id: int,
    content: str,
    view: discord.ui.View | None = None,
) -> None:
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        if view is None:
            await channel.send(content=content)
        else:
            await channel.send(content=content, view=view)
    else:
        log.warning(
            "channel is not a TextChannel; message dropped",
            channel_id=channel_id,
            channel_type=type(channel).__name__,
        )


async def main() -> None:
    if not settings.discord_bot_token:
        # Mirrors the enricher fail-fast: surface missing config at startup,
        # not when the first NOTIFY arrives.
        log.error("DISCORD_BOT_TOKEN not configured")
        sys.exit("DISCORD_BOT_TOKEN not configured")
    bot = CarbuyerBot()
    async with bot:
        await bot.start(settings.discord_bot_token)
