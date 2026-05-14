"""Tests for the Phase 8 vision pass in OpenAIProvider.

Mirrors the mock pattern from test_openai_provider.py — stub
`client.chat.completions.parse` so no real API calls happen.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from carbuyer.llm.base import VisionInput
from carbuyer.llm.openai_provider import OpenAIProvider
from carbuyer.llm.schemas import PerImageOutput, VisionOutput

# ─── fixtures ────────────────────────────────────────────────────────────────


def _per_image_fixture() -> PerImageOutput:
    return PerImageOutput(
        shot_type="exterior_front",
        image_quality_sharpness="sharp",
        image_quality_lighting="well_lit",
        image_quality_cleanliness="clean",
        visible_panels=["hood", "front_bumper"],
        findings=[],
        explicit_unknowns=[],
    )


def _vision_output_fixture() -> VisionOutput:
    return VisionOutput(
        coverage_gaps=["no engine bay shot"],
        cross_panel_paint_consistency="consistent",
        staging_signals=[],
        overall_red_flags=[],
        overall_green_flags=[],
        exterior_condition="decent",
        interior_condition="decent",
        overall_vision_condition="decent",
        vision_confidence=0.7,
        contradictions_with_description=[],
    )


def _vision_input(photo_paths: list[str]) -> VisionInput:
    return VisionInput(
        lot_id=42,
        photo_paths=photo_paths,
        year=2015,
        make="Toyota",
        model="Tacoma",
        description_condition="good",
        description_red_flags=["rust"],
        description_green_flags=["new_tires"],
    )


def _provider() -> OpenAIProvider:
    provider = OpenAIProvider(api_key="sk-fake")
    provider.client = MagicMock()
    return provider


def _fake_response(parsed: object) -> MagicMock:
    fake_choice = MagicMock()
    fake_choice.message.parsed = parsed
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    return fake_response


def _make_jpeg_file(tmp_path: Path, name: str = "photo.jpg") -> Path:
    """Write a minimal valid JPEG so Path.read_bytes() has something to read."""
    # Smallest possible JPEG: SOI + EOI markers.
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9")
    return p


# ─── happy path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_happy_path_two_photos(tmp_path: Path) -> None:
    """2 per-image calls + 1 aggregation call → VisionOutput returned."""
    p1 = _make_jpeg_file(tmp_path, "a.jpg")
    p2 = _make_jpeg_file(tmp_path, "b.jpg")

    per_image = _per_image_fixture()
    agg = _vision_output_fixture()

    provider = _provider()
    provider.client.chat.completions.parse = AsyncMock(
        side_effect=[
            _fake_response(per_image),
            _fake_response(per_image),
            _fake_response(agg),
        ],
    )

    result = await provider.vision(_vision_input([str(p1), str(p2)]))

    assert isinstance(result, VisionOutput)
    assert result.exterior_condition == "decent"
    # 2 per-image + 1 aggregation = 3 total calls
    assert provider.client.chat.completions.parse.await_count == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_vision_multimodal_message_structure(tmp_path: Path) -> None:
    """Per-image call carries a 2-element messages list: system text + user
    multimodal content (text part + image_url part with data URI)."""
    p = _make_jpeg_file(tmp_path)
    img_bytes = p.read_bytes()
    expected_data_url = f"data:image/jpeg;base64,{base64.b64encode(img_bytes).decode()}"

    provider = _provider()
    provider.client.chat.completions.parse = AsyncMock(
        side_effect=[
            _fake_response(_per_image_fixture()),
            _fake_response(_vision_output_fixture()),
        ],
    )

    await provider.vision(_vision_input([str(p)]))

    # First call is the per-image call.
    first_call_kwargs = provider.client.chat.completions.parse.await_args_list[0].kwargs
    messages = first_call_kwargs["messages"]
    assert len(messages) == 2  # noqa: PLR2004
    assert messages[0]["role"] == "system"

    user_msg = messages[1]
    assert user_msg["role"] == "user"
    user_content = user_msg["content"]
    assert len(user_content) == 2  # noqa: PLR2004
    assert user_content[0]["type"] == "text"
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"] == expected_data_url
    # detail=low keeps input tokens ~85/image vs ~1100 at high (default
    # "auto" routes 1024px JPEGs to high). Lock in the explicit cost-saver.
    assert user_content[1]["image_url"]["detail"] == "low"


# ─── per-image partial failure ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_per_image_partial_failure_continues(tmp_path: Path) -> None:
    """1 of 3 per-image calls raises → aggregation still runs with 2 results."""
    p1 = _make_jpeg_file(tmp_path, "a.jpg")
    p2 = _make_jpeg_file(tmp_path, "b.jpg")
    p3 = _make_jpeg_file(tmp_path, "c.jpg")

    per_image = _per_image_fixture()
    agg = _vision_output_fixture()

    provider = _provider()
    provider.client.chat.completions.parse = AsyncMock(
        side_effect=[
            _fake_response(per_image),  # photo 1: ok
            RuntimeError("model refused"),  # photo 2: fails
            _fake_response(per_image),  # photo 3: ok
            _fake_response(agg),  # aggregation
        ],
    )

    result = await provider.vision(_vision_input([str(p1), str(p2), str(p3)]))

    assert isinstance(result, VisionOutput)
    # 3 per-image attempts + 1 aggregation = 4 total calls
    assert provider.client.chat.completions.parse.await_count == 4  # noqa: PLR2004


# ─── per-image returns None parsed ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_per_image_none_parsed_skipped(tmp_path: Path) -> None:
    """When per-image parse returns None, no entry is added; aggregation still
    runs (with an empty per_image_results list)."""
    p = _make_jpeg_file(tmp_path)
    agg = _vision_output_fixture()

    provider = _provider()
    # _parse_to raises RuntimeError when parsed is None, so simulate that.
    provider.client.chat.completions.parse = AsyncMock(
        side_effect=[
            RuntimeError("openai vision_per_image returned no parsed payload"),
            _fake_response(agg),
        ],
    )

    result = await provider.vision(_vision_input([str(p)]))

    assert isinstance(result, VisionOutput)
    # 1 per-image attempt (fails/skipped) + 1 aggregation = 2 total calls
    assert provider.client.chat.completions.parse.await_count == 2  # noqa: PLR2004


# ─── aggregation failure propagates ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_aggregation_failure_propagates(tmp_path: Path) -> None:
    """Aggregation exception must not be swallowed — the worker handles it."""
    p = _make_jpeg_file(tmp_path)

    provider = _provider()
    provider.client.chat.completions.parse = AsyncMock(
        side_effect=[
            _fake_response(_per_image_fixture()),
            RuntimeError("aggregation boom"),
        ],
    )

    with pytest.raises(RuntimeError, match="aggregation boom"):
        await provider.vision(_vision_input([str(p)]))


# ─── aggregation returns None parsed ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_aggregation_none_parsed_raises_runtime_error(
    tmp_path: Path,
) -> None:
    """_parse_to raises RuntimeError when the aggregation model returns None."""
    p = _make_jpeg_file(tmp_path)

    none_response = _fake_response(None)  # parsed=None
    provider = _provider()
    provider.client.chat.completions.parse = AsyncMock(
        side_effect=[
            _fake_response(_per_image_fixture()),
            none_response,
        ],
    )

    with pytest.raises(RuntimeError, match="returned no parsed payload"):
        await provider.vision(_vision_input([str(p)]))


# ─── vehicle line None-safety ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_none_year_make_model_uses_question_mark(tmp_path: Path) -> None:
    """When year/make/model are None, the per-image text prompt uses '?'
    instead of 'None'."""
    p = _make_jpeg_file(tmp_path)

    provider = _provider()
    provider.client.chat.completions.parse = AsyncMock(
        side_effect=[
            _fake_response(_per_image_fixture()),
            _fake_response(_vision_output_fixture()),
        ],
    )

    await provider.vision(
        VisionInput(
            lot_id=None,
            photo_paths=[str(p)],
            year=None,
            make=None,
            model=None,
            description_condition=None,
            description_red_flags=[],
            description_green_flags=[],
        )
    )

    first_call_kwargs = provider.client.chat.completions.parse.await_args_list[0].kwargs
    user_content = first_call_kwargs["messages"][1]["content"]
    text_part = user_content[0]["text"]
    assert "None" not in text_part
    assert "? ? ?." in text_part


# ─── empty photo_paths ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_empty_photo_paths_runs_aggregation_only() -> None:
    """Zero photos → per-image loop runs 0 times, aggregation runs once.

    Batcher guards against this upstream by writing SKIPPED before calling
    vision(), but the provider is a public ABC method and could be called
    from scripts or future callers without the guard. Aggregation must
    handle an empty findings list gracefully.
    """
    provider = _provider()
    provider.client.chat.completions.parse = AsyncMock(
        side_effect=[
            _fake_response(_vision_output_fixture()),
        ],
    )

    result = await provider.vision(_vision_input([]))

    assert isinstance(result, VisionOutput)
    # Only the aggregation call ran; no per-image calls.
    assert provider.client.chat.completions.parse.await_count == 1
