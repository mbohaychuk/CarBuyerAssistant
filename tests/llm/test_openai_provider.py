from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from carbuyer.llm.base import DescribeInput
from carbuyer.llm.openai_provider import DESCRIBE_MAX_TOKENS, OpenAIProvider
from carbuyer.llm.schemas import (
    EnrichmentOutput,
    NormalizedVehicle,
    RarityAssessment,
)


def _enrichment_fixture() -> EnrichmentOutput:
    return EnrichmentOutput(
        normalized_vehicle=NormalizedVehicle(
            year=2010, make="Ford", model="F-150", trim=None, engine="5.4L",
            transmission="automatic", drivetrain="4wd",
            mileage_km=250000, mileage_is_verified=None, vin=None,
        ),
        title_status="NORMAL",
        condition_categorical="decent",
        condition_confidence=0.6,
        red_flags=[], green_flags=[], showstopper_flags=[], concerns=[],
        carfax_url=None,
        summary="ok",
        description_quality="adequate",
        rarity=RarityAssessment(
            desirable_trim_or_spec=False,
            classic_or_collector=False,
            desirability_signals=[], desirability_evidence=[],
        ),
    )


def _describe_input(lot_id: int = 1) -> DescribeInput:
    return DescribeInput(
        lot_id=lot_id,
        title="2010 Ford F-150 4x4",
        description="runs and drives",
        year=2010, make="Ford", model="F-150",
        auctioneer_name=None,
        auction_subtype="estate",
        pickup_province="AB",
        raw_carfax_url=None,
        current_high_bid_cad=Decimal("3200"),
        bid_increment=Decimal("100"),
        auction_close_at=datetime(2026, 5, 15, 18, tzinfo=UTC),
        is_no_reserve=False,
        image_count=4,
        current_year=2026,
    )


def _provider_with_mock(parsed: object, usage: object | None = None) -> OpenAIProvider:
    """Construct provider with stubbed AsyncOpenAI client. Avoids real HTTP."""
    provider = OpenAIProvider(api_key="sk-fake")
    fake_choice = MagicMock()
    fake_choice.message.parsed = parsed
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = usage or MagicMock(
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
    )
    provider.client = MagicMock()
    provider.client.chat.completions.parse = AsyncMock(return_value=fake_response)
    return provider


@pytest.mark.asyncio
async def test_describe_returns_parsed_enrichment_output() -> None:
    expected = _enrichment_fixture()
    provider = _provider_with_mock(expected)
    out = await provider.describe(_describe_input())
    assert out.normalized_vehicle.year == 2010  # noqa: PLR2004
    assert out.condition_categorical == "decent"
    assert out.description_quality == "adequate"


@pytest.mark.asyncio
async def test_describe_calls_ga_path_not_beta() -> None:
    """Phase 3 design overlay #8: GA `client.chat.completions.parse`."""
    provider = _provider_with_mock(_enrichment_fixture())
    await provider.describe(_describe_input())
    # Mock should record the call on chat.completions.parse, not .beta.
    assert provider.client.chat.completions.parse.await_count == 1


@pytest.mark.asyncio
async def test_describe_passes_max_completion_tokens_for_reasoning_model() -> None:
    """GPT-5 family renamed `max_tokens` → `max_completion_tokens`. Default
    model is gpt-5-nano (reasoning), so the new param name must appear and
    the legacy one must NOT (sending both 400s the API)."""
    provider = _provider_with_mock(_enrichment_fixture())
    await provider.describe(_describe_input())
    call_kwargs = provider.client.chat.completions.parse.await_args.kwargs
    assert call_kwargs["max_completion_tokens"] == DESCRIBE_MAX_TOKENS
    assert "max_tokens" not in call_kwargs


@pytest.mark.asyncio
async def test_describe_uses_legacy_max_tokens_for_gpt_4o_mini() -> None:
    """gpt-4o-mini still uses the legacy `max_tokens` + `temperature=0`.
    Dispatch must restore the old shape when the model name doesn't match
    a reasoning-family prefix."""
    provider = _provider_with_mock(_enrichment_fixture())
    provider.model = "gpt-4o-mini"
    await provider.describe(_describe_input())
    call_kwargs = provider.client.chat.completions.parse.await_args.kwargs
    assert call_kwargs["max_tokens"] == DESCRIBE_MAX_TOKENS
    assert call_kwargs["temperature"] == 0
    assert "max_completion_tokens" not in call_kwargs
    assert "reasoning_effort" not in call_kwargs


@pytest.mark.asyncio
async def test_describe_uses_cached_system_prompt() -> None:
    """Phase 3 design overlay #13: system prompt assembled once at
    construction, identical across calls (enables OpenAI prompt cache)."""
    provider = _provider_with_mock(_enrichment_fixture())
    await provider.describe(_describe_input(lot_id=1))
    sys1 = provider.client.chat.completions.parse.await_args.kwargs["messages"][0]["content"]

    provider.client.chat.completions.parse = AsyncMock(
        return_value=provider.client.chat.completions.parse.return_value,
    )
    await provider.describe(_describe_input(lot_id=2))
    sys2 = provider.client.chat.completions.parse.await_args.kwargs["messages"][0]["content"]
    assert sys1 == sys2


@pytest.mark.asyncio
async def test_describe_user_prompt_includes_title_and_context() -> None:
    provider = _provider_with_mock(_enrichment_fixture())
    await provider.describe(_describe_input())
    user = provider.client.chat.completions.parse.await_args.kwargs["messages"][1]["content"]
    assert "2010 Ford F-150 4x4" in user
    assert "current_year: 2026" in user


@pytest.mark.asyncio
async def test_describe_omits_temperature_for_reasoning_model() -> None:
    """GPT-5 family rejects temperature=0 (only temperature=1 supported).
    Dispatch must omit the parameter so the SDK uses the model's default."""
    provider = _provider_with_mock(_enrichment_fixture())
    await provider.describe(_describe_input())
    call_kwargs = provider.client.chat.completions.parse.await_args.kwargs
    assert "temperature" not in call_kwargs


@pytest.mark.asyncio
async def test_provider_async_context_manager_closes_client() -> None:
    """Phase 3 design overlay #27."""
    provider = _provider_with_mock(_enrichment_fixture())
    provider.client.close = AsyncMock()
    async with provider as p:
        assert p is provider
    provider.client.close.assert_awaited_once()



def test_openai_provider_constructor_passes_retry_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 design overlay #9: SDK-managed retries + timeout."""
    captured: dict[str, object] = {}

    def fake_async_openai(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(
        "carbuyer.llm.openai_provider.AsyncOpenAI", fake_async_openai,
    )
    OpenAIProvider(api_key="sk-fake", max_retries=7, timeout=30.0)
    assert captured["max_retries"] == 7  # noqa: PLR2004
    assert captured["timeout"] == 30.0  # noqa: PLR2004
