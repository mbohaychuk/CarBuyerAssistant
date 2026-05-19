"""Phase 5 Task 32 — persistent action buttons + bot fail-fast tests.

Pure-Python; no Discord gateway. The discord.py runtime (gateway, websocket,
interaction dispatch) is integration territory and lives in manual smoke
testing. These tests cover the offline-verifiable bits:

  * ``DynamicItem.from_custom_id`` parses the regex match into a button
    with the correct ``lot_id`` and the right action's ``custom_id``
  * ``CarbuyerBot`` instantiates with ``guilds``-only intents
  * ``bot.main()`` exits when ``DISCORD_BOT_TOKEN`` is unset
  * ``_set_user_action`` returns False when the lot is missing
"""
from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.bot import views as views_mod
from carbuyer.apps.bot.bot import CarbuyerBot, main
from carbuyer.apps.bot.views import (
    LotInterestedButton,
    LotMaybeButton,
    LotNotInterestedButton,
    _set_user_action,  # pyright: ignore[reportPrivateUsage]
)
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot


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
    # Use the class's own compiled template — guards against future template
    # renames (e.g. "deal:" → "lot:") that would otherwise leave the test
    # passing against a hand-rolled regex while real dispatch breaks.
    # ``DynamicItem`` stores the compiled pattern at this canonical name; the
    # public ``template`` is an instance property returning the same pattern.
    template: re.Pattern[str] = cls.__discord_ui_compiled_template__
    match = template.fullmatch(custom_id)
    assert match is not None, (
        f"class template {template.pattern!r} did not match {custom_id!r}"
    )
    instance = await cls.from_custom_id(MagicMock(), MagicMock(), match)
    assert isinstance(instance, cls)
    assert instance.lot_id == lot_id
    assert instance.custom_id == custom_id


def test_intents_only_guilds() -> None:
    bot = CarbuyerBot()
    assert bot.intents.guilds is True
    # Every other intent must be off — the bot doesn't read messages, members,
    # presences, voice, etc. Smoke-check a representative slice.
    assert bot.intents.members is False
    assert bot.intents.message_content is False
    assert bot.intents.presences is False
    assert bot.intents.voice_states is False
    assert bot.intents.guild_messages is False


@pytest.mark.asyncio
async def test_main_exits_when_discord_bot_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "carbuyer.apps.bot.bot.settings.discord_bot_token", "",
    )
    with pytest.raises(SystemExit, match="DISCORD_BOT_TOKEN not configured"):
        await main()


# ─── _set_user_action ───


@pytest.fixture
def _patched_get_session(  # pyright: ignore[reportUnusedFunction]
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    """Make ``views.get_session()`` reuse the test connection so the helper's
    inner transaction becomes a savepoint under the test's outer rollback."""
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(views_mod, "get_session", fake_get_session)
    return session


def _seed_lot(session: AsyncSession) -> AuctionLot:
    a = Auction(
        source="test", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        pickup_province="AB",
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
    )
    session.add(a)
    lot = AuctionLot(
        auction=a, source_lot_id="L1", url="https://x/lot/L1",
        title="2015 Toyota Tacoma",
    )
    session.add(lot)
    return lot


@pytest.mark.asyncio
async def test_set_user_action_writes_when_lot_exists(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    lot = _seed_lot(session)
    await session.flush()
    lot_id = lot.id

    ok = await _set_user_action(lot_id, UserAction.INTERESTED)

    assert ok is True
    await session.refresh(lot)
    assert lot.user_action == "interested"


@pytest.mark.asyncio
async def test_set_user_action_logs_on_success(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    lot = _seed_lot(session)
    await session.flush()
    lot_id = lot.id

    infos: list[tuple[str, dict[str, object]]] = []

    def capture_info(event: str, **kw: object) -> None:
        infos.append((event, kw))

    spy = MagicMock()
    spy.info = capture_info
    monkeypatch.setattr(views_mod, "log", spy)

    ok = await _set_user_action(lot_id, UserAction.INTERESTED)

    assert ok is True
    assert infos == [
        (
            "user_action written",
            {"lot_id": lot_id, "action": UserAction.INTERESTED},
        ),
    ]


@pytest.mark.asyncio
async def test_set_user_action_returns_false_when_lot_missing(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Spy on the module-level structlog logger so we can assert the warning
    # was emitted with the right event + fields. We don't go through caplog
    # because structlog's default JSON renderer goes to stdout, not stdlib.
    warnings: list[tuple[str, dict[str, object]]] = []

    def capture_warning(event: str, **kw: object) -> None:
        warnings.append((event, kw))

    spy = MagicMock()
    spy.warning = capture_warning
    monkeypatch.setattr(views_mod, "log", spy)

    ok = await _set_user_action(999_999, UserAction.INTERESTED)

    assert ok is False
    assert warnings == [
        (
            "user_action write skipped — lot not found",
            {"lot_id": 999_999, "action": UserAction.INTERESTED},
        ),
    ]
