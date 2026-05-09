from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Province = Literal["AB", "BC", "SK", "MB", "ON", "QC", "NS", "NB", "NL", "PE", "YT", "NT", "NU"]


# Default UA. Update each quarter; can be overridden via HTTP_USER_AGENT env.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://carbuyer:local@localhost:5433/carbuyer"
    )
    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="gpt-4o-mini")
    discord_bot_token: str = Field(default="")
    discord_guild_id: int | None = None
    discord_channels: dict[str, int] = Field(default_factory=dict)
    home_province: Province = "AB"

    notify_threshold: float = 0.15
    early_warning_rarity_threshold: float = 2.0
    early_warning_min_hours_to_close: int = 48
    rescore_improvement_threshold: float = 0.05

    quiet_hours_start: int = 22
    quiet_hours_end: int = 8
    quiet_hours_override_score: float = 0.30

    flip_margin_min_cad: int = 1500
    flip_margin_pct: float = 0.10

    log_level: str = "INFO"
    http_user_agent: str = DEFAULT_USER_AGENT

    # HiBid plugin discovery target provinces. Override via env, e.g.
    # `HIBID_PROVINCES='["AB","BC","SK"]'` to scope discovery for testing.
    hibid_provinces: list[Province] = Field(
        default_factory=lambda: ["AB", "BC", "SK", "MB"],
    )

    @field_validator("discord_channels", mode="before")
    @classmethod
    def _parse_discord_channels(cls, value: Any) -> dict[str, int]:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("DISCORD_CHANNELS must be a JSON object of name→channel_id")
        result: dict[str, int] = {}
        for k, v in value.items():  # type: ignore[reportUnknownVariableType]
            result[str(k)] = int(v)  # type: ignore[reportUnknownArgumentType]
        return result


settings = Settings()
