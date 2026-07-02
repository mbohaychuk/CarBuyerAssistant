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

    database_url: str = Field(default="postgresql+psycopg://carbuyer:local@localhost:5433/carbuyer")
    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="gpt-5-nano")
    # Phase 3 design overlay #9: SDK-managed retries + per-call timeout. Empty
    # API key triggers fail-fast at worker startup, not on first call.
    openai_max_retries: int = 5
    openai_request_timeout_s: float = 60.0
    # Reasoning-token effort for GPT-5 / o-series models. None = SDK default
    # (medium), which burns hundreds of invisible reasoning tokens per call
    # even on trivial schemas. Our workload is extraction/classification —
    # "low" is sufficient and cuts effective output cost 3-5x. Set to None
    # only when running a non-reasoning model (gpt-4o-mini, etc.).
    openai_reasoning_effort: str | None = "low"
    # Phase 3 design overlay #12: bounded LLM concurrency per worker. tier-1
    # gpt-5-nano caps at 500 RPM / 200K TPM; 4-way is conservative.
    openai_concurrency: int = 4
    enrichment_batch_size: int = 20
    # Phase 3 design overlay #4 + #26: enrichment retry counter. Transient
    # errors (rate limits, 5xx, network) leave status PENDING for re-claim
    # until attempts >= this; schema/validation errors fail-fast at attempts=1.
    enrichment_max_attempts: int = 3
    # Phase 3 design overlay #14: bumped on every prompt/taxonomy change so a
    # backfill can re-pend stale rows: UPDATE auction_lots SET
    # enrichment_status='pending' WHERE enrichment_version IS DISTINCT FROM 'vN'.
    enrichment_version: str = "v1"
    discord_bot_token: str = Field(default="")
    discord_guild_id: int | None = None
    # Values can be int channel IDs OR string channel names. Names are
    # resolved via channel_resolver.resolve_channels() at notifier startup.
    discord_channels: dict[str, int | str] = Field(default_factory=dict)
    home_province: Province = "AB"

    # Tiered delivery (§5d): want_relative_score at/above which a match pings
    # instantly; auction lots closing within instant_closing_hours ping too;
    # quiet hours defer the instant tier into the next morning digest.
    instant_deal_threshold: float = 0.15
    instant_closing_hours: int = 24
    quiet_hours_start: int = 22
    quiet_hours_end: int = 8

    # Phase 4 valuator. batch_size mirrors enrichment_batch_size but the
    # valuator does no LLM I/O, so it can drain larger batches.
    valuation_batch_size: int = 30
    valuation_max_attempts: int = 3
    # Phase 13: notifier retry cap. A Discord POST returning False
    # (429-after-retry, 4xx, network blip, missing channel) leaves
    # notification_status=PENDING and increments notification_attempts;
    # once attempts >= this, the worker flips to FAILED. Mirrors the
    # enrichment/valuation retry semantics.
    notification_max_attempts: int = 3
    notification_batch_size: int = 50
    # Phase 4 overlay #12: lots whose RAW cumulative red-flag weight (pre-clip,
    # pre-dilution-cap) is at or below this are excluded from notifications
    # regardless of price-deal score. Heuristic; revisit after first 100 lots.
    excessive_red_flag_weight_threshold: int = -8
    # Phase 4 scoring version — bump on any change to scoring formula or
    # weight tables so a backfill can re-pend stale rows.
    scoring_version: str = "v1"

    # Phase 8 vision-batcher knobs. Threshold gates how aggressive the nightly
    # vision pass is (lower = more lots inspected = more LLM cost). Limit caps
    # one nightly run; bump if the cron window is wide enough to absorb more.
    # Both are env-tunable so ops can throttle without a code deploy.
    vision_shortlist_score_threshold: float = 0.10
    vision_shortlist_limit: int = 100

    log_level: str = "INFO"
    http_user_agent: str = DEFAULT_USER_AGENT
    # Private-source scraping substrate (Phase 1 S4). curl_cffi TLS-impersonation
    # target (e.g. "chrome", "chrome124", "safari17_0") and an optional CA
    # residential proxy URL. Anti-bot sites fingerprint the TLS handshake, not
    # just the UA, so the proxy alone isn't enough without impersonation.
    http_impersonate: str = "chrome"
    proxy_url: str | None = None
    # Opt-in NHTSA vPIC make/model normalization in the enricher (adds one
    # keyless HTTP call per lot; best-effort, off by default). Aligns comp keys
    # across LLM output + scraped listings ("F150" → "F-150").
    vpic_normalization_enabled: bool = False
    # Opt-in NHTSA reliability lookup in the enricher (recall + complaint counts;
    # two keyless HTTP calls per lot; best-effort, off by default).
    nhtsa_reliability_enabled: bool = False

    # HiBid discovery target provinces: western + central Canada + territories.
    # Atlantic provinces are mapped (sources.hibid.urls.PROVINCE_PATH) but off by
    # default (far from home, high landed cost). Override via env, e.g.
    # `HIBID_PROVINCES='["AB","BC","SK"]'` to scope discovery for testing.
    hibid_provinces: list[Province] = Field(
        default_factory=lambda: ["AB", "BC", "SK", "MB", "ON", "QC", "YT", "NT", "NU"],
    )

    # Craigslist regions (subdomains) the source searches; a want scoped to
    # provinces only hits regions in those provinces. Override via env, e.g.
    # `CRAIGSLIST_REGIONS='["calgary","edmonton"]'`.
    craigslist_regions: list[str] = Field(
        default_factory=lambda: [
            "calgary", "edmonton", "vancouver", "victoria", "kelowna",
            "winnipeg", "toronto", "ottawa", "hamilton", "kitchener",
            "london", "windsor", "montreal", "halifax", "saskatoon", "regina",
        ],
    )

    @field_validator("discord_channels", mode="before")
    @classmethod
    def _parse_discord_channels(cls, value: Any) -> dict[str, int | str]:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError(
                "DISCORD_CHANNELS must be a JSON object of key→(channel_id or name)",
            )
        result: dict[str, int | str] = {}
        for k, v in value.items():  # type: ignore[reportUnknownVariableType]
            key = str(k)  # type: ignore[reportUnknownArgumentType]
            if isinstance(v, int):
                result[key] = v
            elif isinstance(v, str) and v.lstrip("#").isdigit():
                # Numeric-string IDs ("12345") are normalized to int up front.
                result[key] = int(v.lstrip("#"))
            else:
                result[key] = str(v)  # type: ignore[reportUnknownArgumentType]
        return result


settings = Settings()
