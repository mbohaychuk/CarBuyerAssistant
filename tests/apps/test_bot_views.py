"""Phase 5 Task 32 — persistent action buttons + bot fail-fast tests.

Pure-Python; no Discord gateway. The discord.py runtime (gateway, websocket,
interaction dispatch) is integration territory and lives in manual smoke
testing. These tests cover the offline-verifiable bits:

  * ``build_view_for_lot`` returns a 3-button view
  * ``DynamicItem.from_custom_id`` parses the regex match into a button
    with the correct ``lot_id`` and the right action's ``custom_id``
  * ``make_intents()`` enables only ``guilds`` and nothing else
  * ``bot.main()`` exits when ``DISCORD_BOT_TOKEN`` is unset
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from carbuyer.apps.bot.bot import main, make_intents
from carbuyer.apps.bot.views import (
    LotInterestedButton,
    LotMaybeButton,
    LotNotInterestedButton,
    build_view_for_lot,
)


def test_build_view_for_lot_has_three_buttons() -> None:
    v = build_view_for_lot(1)
    assert len(v.children) == 3  # noqa: PLR2004


def test_build_view_for_lot_distinct_custom_ids() -> None:
    v = build_view_for_lot(42)
    custom_ids = {c.custom_id for c in v.children}  # type: ignore[attr-defined]
    assert custom_ids == {
        "deal:interested:42",
        "deal:maybe:42",
        "deal:not_interested:42",
    }


def test_lot_action_view_persistent() -> None:
    # Persistent views require timeout=None and all-children-have-custom_id;
    # is_persistent() returns False if either condition fails.
    assert build_view_for_lot(7).is_persistent() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cls", "action", "lot_id"),
    [
        (LotInterestedButton, "interested", 99),
        (LotMaybeButton, "maybe", 12345),
        (LotNotInterestedButton, "not_interested", 1),
    ],
)
async def test_from_custom_id_parses_lot_id(
    cls: type[LotInterestedButton] | type[LotMaybeButton] | type[LotNotInterestedButton],
    action: str,
    lot_id: int,
) -> None:
    custom_id = f"deal:{action}:{lot_id}"
    match = re.fullmatch(rf"deal:{action}:(?P<lot_id>\d+)", custom_id)
    assert match is not None
    instance = await cls.from_custom_id(MagicMock(), MagicMock(), match)
    assert isinstance(instance, cls)
    assert instance.lot_id == lot_id
    assert instance.custom_id == custom_id


def test_intents_only_guilds() -> None:
    intents = make_intents()
    assert intents.guilds is True
    # Every other intent must be off — the bot doesn't read messages, members,
    # presences, voice, etc. Smoke-check a representative slice.
    assert intents.members is False
    assert intents.message_content is False
    assert intents.presences is False
    assert intents.voice_states is False
    assert intents.guild_messages is False


@pytest.mark.asyncio
async def test_main_exits_when_discord_bot_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "carbuyer.apps.bot.bot.settings.discord_bot_token", "",
    )
    with pytest.raises(SystemExit, match="DISCORD_BOT_TOKEN not configured"):
        await main()
