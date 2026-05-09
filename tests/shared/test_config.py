import pytest
from pydantic import ValidationError

from carbuyer.shared.config import Settings

DEFAULT_NOTIFY_THRESHOLD = 0.15
DEFAULT_EARLY_WARNING_HOURS = 48


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5433/db")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("HOME_PROVINCE", "AB")
    s = Settings()
    assert s.database_url.endswith("/db")
    assert s.openai_api_key == "sk-test"
    assert s.discord_bot_token == "tok"
    assert s.home_province == "AB"
    assert s.notify_threshold == DEFAULT_NOTIFY_THRESHOLD
    assert s.early_warning_min_hours_to_close == DEFAULT_EARLY_WARNING_HOURS


def test_discord_channels_parses_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_CHANNELS", '{"early_warning": 111, "hot_deals": 222}')
    s = Settings()
    assert s.discord_channels == {"early_warning": 111, "hot_deals": 222}


def test_discord_channels_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_CHANNELS", raising=False)
    s = Settings()
    assert s.discord_channels == {}


def test_discord_channels_rejects_non_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_CHANNELS", "[1, 2, 3]")
    with pytest.raises(ValidationError):
        Settings()


def test_http_user_agent_has_default() -> None:
    s = Settings()
    assert "Mozilla" in s.http_user_agent
