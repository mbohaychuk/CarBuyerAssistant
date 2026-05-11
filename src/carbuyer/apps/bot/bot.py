"""discord.py runtime for the notifier bot.

The bot owns the persistent gateway connection so button interactions on
notifications dispatch to ``DynamicItem`` callbacks (registered in
``setup_hook``). Notifications themselves are posted by the Phase 6 notifier
worker via direct REST POST (``apps/notifier/discord_post.py``); this bot
does not send messages itself.

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

    async def on_ready(self) -> None:
        log.info(
            "bot ready",
            user=str(self.user),
            guild_count=len(self.guilds),
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
