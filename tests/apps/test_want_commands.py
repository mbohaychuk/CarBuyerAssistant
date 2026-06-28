from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.bot import want_commands as wc
from carbuyer.apps.bot.bot import CarbuyerBot
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria


@pytest.fixture
def _patched(  # pyright: ignore[reportUnusedFunction]
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(wc, "get_session", fake_get_session)
    return session


async def test_add_want_persists_with_parsed_criteria(_patched: AsyncSession) -> None:
    session = _patched
    msg = await wc.add_want(
        name="manual xterra",
        makes="Nissan",
        models="Xterra",
        transmissions="manual",
        year_min=2005,
        year_max=2015,
        max_price_cad=15000,
        provinces="AB, BC",
    )
    assert "Added" in msg

    wants = await repo.list_wants(session)
    assert [w.name for w in wants] == ["manual xterra"]
    crit = WantCriteria.model_validate(wants[0].config)
    assert crit.makes == ["Nissan"]
    assert crit.transmissions == ["manual"]
    assert crit.provinces == ["AB", "BC"]
    assert crit.year_min == 2005  # noqa: PLR2004 -- explicit input value


async def test_add_want_rejects_invalid_transmission(_patched: AsyncSession) -> None:
    session = _patched
    msg = await wc.add_want(name="x", transmissions="stick")
    assert "invalid" in msg.lower()
    assert await repo.list_wants(session) == []


async def test_add_want_rejects_bad_year_range(_patched: AsyncSession) -> None:
    msg = await wc.add_want(name="x", year_min=2015, year_max=2005)
    assert "invalid" in msg.lower()


async def test_list_wants_text_empty(_patched: AsyncSession) -> None:
    assert "No wants" in await wc.list_wants_text()


async def test_list_wants_text_shows_names(_patched: AsyncSession) -> None:
    await wc.add_want(name="alpha")
    await wc.add_want(name="beta")
    text = await wc.list_wants_text()
    assert "alpha" in text
    assert "beta" in text


async def test_remove_want(_patched: AsyncSession) -> None:
    session = _patched
    await wc.add_want(name="x")
    want_id = (await repo.list_wants(session))[0].id
    assert "Removed" in await wc.remove_want(want_id)
    assert await repo.list_wants(session) == []
    assert "No want" in await wc.remove_want(want_id)


async def test_mute_and_unmute_want(_patched: AsyncSession) -> None:
    session = _patched
    await wc.add_want(name="x")
    want_id = (await repo.list_wants(session))[0].id

    assert "Muted" in await wc.set_want_enabled(want_id, enabled=False)
    assert await repo.list_wants(session, enabled_only=True) == []
    assert "Unmuted" in await wc.set_want_enabled(want_id, enabled=True)
    assert [w.id for w in await repo.list_wants(session, enabled_only=True)] == [want_id]


async def test_set_enabled_missing_want(_patched: AsyncSession) -> None:
    assert "No want" in await wc.set_want_enabled(999_999, enabled=False)


def test_want_group_registered_on_bot() -> None:
    bot = CarbuyerBot()
    group = bot.tree.get_command("want")
    assert group is not None
    assert isinstance(group, wc.WantGroup)
    subcommands = {c.name for c in group.walk_commands()}
    assert {"add", "list", "remove", "mute", "unmute"} <= subcommands
