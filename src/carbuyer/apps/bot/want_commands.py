"""`/want` slash commands for managing the want-list.

The handlers (add_want / list_wants_text / remove_want / set_want_enabled) take
primitives, do the repo work, and return the reply string — pure of discord.py so
they unit-test like views._set_user_action. WantGroup below is the thin
app_commands wrapper the bot registers; its interaction dispatch is integration
territory (manual smoke testing), so the logic worth testing lives in the handlers.
"""
from __future__ import annotations

import discord
from discord import app_commands
from pydantic import ValidationError

from carbuyer.db.session import get_session
from carbuyer.shared.logging import get_logger
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria

log = get_logger("bot")


def _split(value: str | None) -> list[str]:
    """Comma-separated slash-command input → list, dropping blanks."""
    return [part.strip() for part in value.split(",") if part.strip()] if value else []


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    msg = err.get("msg", "invalid value")
    return f"{loc}: {msg}" if loc else msg


async def add_want(
    *,
    name: str,
    makes: str | None = None,
    models: str | None = None,
    trims: str | None = None,
    transmissions: str | None = None,
    drivetrains: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    max_price_cad: int | None = None,
    max_mileage_km: int | None = None,
    provinces: str | None = None,
    condition_min: str | None = None,
) -> str:
    # model_validate (not the typed ctor) so runtime-validated strings from the
    # slash command don't trip the static list[Literal] field types.
    try:
        criteria = WantCriteria.model_validate({
            "makes": _split(makes),
            "models": _split(models),
            "trims": _split(trims),
            "transmissions": _split(transmissions),
            "drivetrains": _split(drivetrains),
            "year_min": year_min,
            "year_max": year_max,
            "price_ceiling_cad": max_price_cad,
            "max_mileage_km": max_mileage_km,
            "provinces": _split(provinces),
            "condition_min": condition_min or None,
        })
    except ValidationError as exc:
        return f"Invalid want: {_first_error(exc)}"

    async with get_session() as session, session.begin():
        want = await repo.create_want(session, name=name, criteria=criteria)
        return f"Added want #{want.id}: {name}"


async def list_wants_text() -> str:
    async with get_session() as session:
        wants = await repo.list_wants(session)
    if not wants:
        return "No wants yet — use `/want add` to create one."
    return "\n".join(
        f"#{w.id} {'\U0001f514' if w.enabled else '\U0001f515'} {w.name}" for w in wants
    )


async def remove_want(want_id: int) -> str:
    async with get_session() as session, session.begin():
        removed = await repo.delete_want(session, want_id)
    return f"Removed want #{want_id}." if removed else f"No want #{want_id}."


async def set_want_enabled(want_id: int, *, enabled: bool) -> str:
    async with get_session() as session, session.begin():
        want = await repo.update_want(session, want_id, enabled=enabled)
    if want is None:
        return f"No want #{want_id}."
    return f"{'Unmuted' if enabled else 'Muted'} want #{want_id}."


class WantGroup(app_commands.Group):
    """The `/want` command group. Thin — each command defers to a handler."""

    def __init__(self) -> None:
        super().__init__(name="want", description="Manage your vehicle want-list")

    @app_commands.command(name="add", description="Add a want to monitor")
    async def add(
        self,
        interaction: discord.Interaction,
        name: str,
        makes: str | None = None,
        models: str | None = None,
        trims: str | None = None,
        transmissions: str | None = None,
        drivetrains: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        max_price_cad: int | None = None,
        max_mileage_km: int | None = None,
        provinces: str | None = None,
        condition_min: str | None = None,
    ) -> None:
        reply = await add_want(
            name=name, makes=makes, models=models, trims=trims,
            transmissions=transmissions, drivetrains=drivetrains,
            year_min=year_min, year_max=year_max, max_price_cad=max_price_cad,
            max_mileage_km=max_mileage_km, provinces=provinces,
            condition_min=condition_min,
        )
        await interaction.response.send_message(reply, ephemeral=True)

    @app_commands.command(name="list", description="List your wants")
    async def list(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(await list_wants_text(), ephemeral=True)

    @app_commands.command(name="remove", description="Delete a want by id")
    async def remove(self, interaction: discord.Interaction, want_id: int) -> None:
        await interaction.response.send_message(await remove_want(want_id), ephemeral=True)

    @app_commands.command(name="mute", description="Pause alerts for a want")
    async def mute(self, interaction: discord.Interaction, want_id: int) -> None:
        reply = await set_want_enabled(want_id, enabled=False)
        await interaction.response.send_message(reply, ephemeral=True)

    @app_commands.command(name="unmute", description="Resume alerts for a want")
    async def unmute(self, interaction: discord.Interaction, want_id: int) -> None:
        reply = await set_want_enabled(want_id, enabled=True)
        await interaction.response.send_message(reply, ephemeral=True)
