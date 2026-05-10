"""Persistent action buttons for lot notifications.

Each notification posted by Phase 6's notifier carries a row of three buttons
("Interested", "Maybe", "Not interested") wired to the lot's row in
``auction_lots.user_action``. The buttons are persistent — they keep working
across bot restarts because each ``custom_id`` encodes the lot id, and
``DynamicItem`` re-instantiates the button from a regex on first interaction
after restart.

We register one ``DynamicItem`` subclass per action rather than parameterising
a single class because discord.py matches the ``template=...`` regex at
registration time per class. The duplicated callback bodies are factored into
``_set_user_action``.
"""
from __future__ import annotations

import re
from typing import Any

import discord
from discord import ButtonStyle, Interaction
from discord.ui import DynamicItem, View

from carbuyer.db.enums import UserAction
from carbuyer.db.models import AuctionLot
from carbuyer.db.session import get_session
from carbuyer.shared.logging import get_logger

log = get_logger("bot")


async def _set_user_action(lot_id: int, action: UserAction) -> bool:
    async with get_session() as session, session.begin():
        lot = await session.get(AuctionLot, lot_id)
        if lot is None:
            log.warning(
                "user_action write skipped — lot not found",
                lot_id=lot_id,
                action=action,
            )
            return False
        lot.user_action = action
        log.info("user_action written", lot_id=lot_id, action=action)
        return True


class LotInterestedButton(
    DynamicItem[discord.ui.Button[View]],
    template=r"deal:interested:(?P<lot_id>\d+)",
):
    def __init__(self, lot_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=ButtonStyle.success,
                label="\U0001f44d Interested",
                custom_id=f"deal:interested:{lot_id}",
            )
        )
        self.lot_id = lot_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> LotInterestedButton:
        return cls(int(match["lot_id"]))

    async def callback(self, interaction: Interaction) -> Any:
        await interaction.response.defer(ephemeral=True)
        ok = await _set_user_action(self.lot_id, UserAction.INTERESTED)
        msg = (
            f"Marked lot {self.lot_id} as interested."
            if ok
            else f"Lot {self.lot_id} not found."
        )
        await interaction.followup.send(msg, ephemeral=True)


class LotMaybeButton(
    DynamicItem[discord.ui.Button[View]],
    template=r"deal:maybe:(?P<lot_id>\d+)",
):
    def __init__(self, lot_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=ButtonStyle.secondary,
                label="\U0001f914 Maybe",
                custom_id=f"deal:maybe:{lot_id}",
            )
        )
        self.lot_id = lot_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> LotMaybeButton:
        return cls(int(match["lot_id"]))

    async def callback(self, interaction: Interaction) -> Any:
        await interaction.response.defer(ephemeral=True)
        ok = await _set_user_action(self.lot_id, UserAction.MAYBE)
        msg = (
            f"Marked lot {self.lot_id} as maybe."
            if ok
            else f"Lot {self.lot_id} not found."
        )
        await interaction.followup.send(msg, ephemeral=True)


class LotNotInterestedButton(
    DynamicItem[discord.ui.Button[View]],
    template=r"deal:not_interested:(?P<lot_id>\d+)",
):
    def __init__(self, lot_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=ButtonStyle.danger,
                label="\U0001f44e Not interested",
                custom_id=f"deal:not_interested:{lot_id}",
            )
        )
        self.lot_id = lot_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> LotNotInterestedButton:
        return cls(int(match["lot_id"]))

    async def callback(self, interaction: Interaction) -> Any:
        await interaction.response.defer(ephemeral=True)
        ok = await _set_user_action(self.lot_id, UserAction.NOT_INTERESTED)
        msg = (
            f"Marked lot {self.lot_id} as not interested."
            if ok
            else f"Lot {self.lot_id} not found."
        )
        await interaction.followup.send(msg, ephemeral=True)


