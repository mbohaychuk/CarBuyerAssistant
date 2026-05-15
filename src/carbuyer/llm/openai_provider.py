"""OpenAI implementation of the LLMProvider role mixins.

Phase 3 design overlay #8: GA `client.chat.completions.parse` (not the legacy
`.beta.` path).

Phase 3 design overlay #9: SDK-managed retries via constructor args. OpenAI
does not bill for retried failed calls so the SDK retry is free reliability.

Phase 3 design overlay #11: per-call usage logging (prompt_tokens,
completion_tokens, total_tokens, duration_ms) feeds the budget signal.

Phase 3 design overlay #13: system prompt assembled once at construction.
The taxonomy is several KB; OpenAI's prompt cache prefers identical prefixes.

Phase 3 design overlay #27: provider is an async context manager so the
worker can `async with OpenAIProvider() as provider:` for clean shutdown.
"""
from __future__ import annotations

import asyncio
import time
from base64 import b64encode
from pathlib import Path
from types import TracebackType
from typing import Self, TypeVar

from openai import APIError, AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from carbuyer.llm.base import (
    DescribeInput,
    LLMProvider,
    VisionInput,
)
from carbuyer.llm.prompts import (
    VISION_AGGREGATION_PROMPT,
    VISION_PER_IMAGE_PROMPT,
    description_system_prompt,
    description_user_prompt,
)
from carbuyer.llm.schemas import EnrichmentOutput, PerImageOutput, VisionOutput
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("openai_provider")

# Phase 3 design overlay #10: 3000-token output ceiling. Worst-case
# EnrichmentOutput with multiple flag entries carrying verbatim evidence quotes
# tokenizes around 1.5-2k completion tokens; 2048 had no margin and truncation
# produces invalid JSON which the schema rejects, marking the lot failed.
DESCRIBE_MAX_TOKENS = 3000

# Phase 8 vision token ceilings. Per-image is single-shot structured output
# (compact schema); aggregation synthesises N per-image JSON blobs into the
# richer VisionOutput.
_VISION_PER_IMAGE_MAX_TOKENS = 512
_VISION_AGGREGATE_MAX_TOKENS = 1024

T = TypeVar("T", bound=BaseModel)


class OpenAIProvider(LLMProvider):
    """OpenAI implementation of `describe` and `vision` (Phase 3 + Phase 8).

    Both methods funnel through `_parse_to`, which is the single chokepoint
    for the structured-output API call shape and per-call usage logging
    (prompt_tokens / completion_tokens / total_tokens / duration_ms tagged
    with a `kind` label: `describe`, `vision_per_image`, `vision_aggregate`).

    The vision pass fans out one per-image LLM call per photo, then a single
    aggregation call over the per-image JSON. Per-image failures (network
    blips, schema-rejected output) are caught and skipped so partial photos
    still aggregate; aggregation failures propagate so the worker can flip
    `vision_status=FAILED`.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.client = AsyncOpenAI(
            api_key=api_key or settings.openai_api_key,
            max_retries=max_retries if max_retries is not None else settings.openai_max_retries,
            timeout=timeout if timeout is not None else settings.openai_request_timeout_s,
        )
        self.model = model or settings.openai_model
        # Cache the static system prompt — large, taxonomy-driven, reused per call.
        self._describe_system_prompt = description_system_prompt()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self.client.close()

    async def _parse_to(
        self,
        *,
        response_format: type[T],
        messages: list[ChatCompletionMessageParam],
        max_tokens: int,
        kind: str,
        lot_id: int | None,
    ) -> T:
        """Single chokepoint for the OpenAI structured-output call shape.

        Accepts an already-assembled messages list so both text-only (describe)
        and multimodal (vision) callers share token-usage + duration logging.
        """
        t0 = time.monotonic()
        # reasoning_effort is only meaningful for GPT-5 / o-series models;
        # leave it off the call when unset so we stay compatible with older
        # models (gpt-4o-mini, gpt-4o) that reject the parameter.
        extra: dict[str, str] = {}
        if settings.openai_reasoning_effort:
            extra["reasoning_effort"] = settings.openai_reasoning_effort
        try:
            response = await self.client.chat.completions.parse(
                model=self.model,
                messages=messages,
                response_format=response_format,
                temperature=0,
                max_tokens=max_tokens,
                **extra,  # type: ignore[arg-type]
            )
        except (APIError, RateLimitError):
            log.exception(
                "openai parse failed",
                kind=kind,
                lot_id=lot_id,
                model=self.model,
            )
            raise
        duration_ms = int((time.monotonic() - t0) * 1000)
        usage = response.usage
        log.info(
            "openai parse",
            kind=kind,
            lot_id=lot_id,
            model=self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            duration_ms=duration_ms,
        )
        result = response.choices[0].message.parsed
        if result is None:
            msg = f"openai {kind} returned no parsed payload"
            raise RuntimeError(msg)
        return result

    async def describe(self, payload: DescribeInput) -> EnrichmentOutput:
        user_prompt = description_user_prompt(
            title=payload.title,
            description=payload.description,
            year=payload.year,
            make=payload.make,
            model=payload.model,
            auctioneer_name=payload.auctioneer_name,
            auction_subtype=payload.auction_subtype,
            pickup_province=payload.pickup_province,
            current_high_bid_cad=payload.current_high_bid_cad,
            bid_increment=payload.bid_increment,
            auction_close_at=payload.auction_close_at,
            is_no_reserve=payload.is_no_reserve,
            image_count=payload.image_count,
            current_year=payload.current_year,
        )
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": self._describe_system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self._parse_to(
            response_format=EnrichmentOutput,
            messages=messages,
            max_tokens=DESCRIBE_MAX_TOKENS,
            kind="describe",
            lot_id=payload.lot_id,
        )

    async def vision(self, payload: VisionInput) -> VisionOutput:
        vehicle_line = (
            f"Vehicle: {payload.year or '?'} {payload.make or '?'} {payload.model or '?'}."
        )
        red_flags_str = ", ".join(payload.description_red_flags) or "(none)"
        green_flags_str = ", ".join(payload.description_green_flags) or "(none)"
        condition_str = payload.description_condition or "(unknown)"

        per_image_results: list[PerImageOutput] = []
        for photo_path in payload.photo_paths:
            # Photos are converted to JPEG by Task 36's `download_and_resize`;
            # if the upstream contract changes, update this MIME type.
            # asyncio.to_thread: Path.read_bytes is sync blocking I/O.
            img_bytes = await asyncio.to_thread(Path(photo_path).read_bytes)
            data_url = f"data:image/jpeg;base64,{b64encode(img_bytes).decode()}"
            per_image_messages: list[ChatCompletionMessageParam] = [
                {"role": "system", "content": VISION_PER_IMAGE_PROMPT},
                {"role": "user", "content": [  # type: ignore[list-item]
                    {"type": "text", "text": (
                        f"{vehicle_line} "
                        f"Description condition (claimed): {condition_str}. "
                        f"Description red flags: {red_flags_str}. "
                        f"Description green flags: {green_flags_str}."
                    )},
                    # detail="low" — ~85 input tokens per image vs ~1100 at
                    # "high" (which is what "auto" routes 1024×1024 to). For
                    # rust/dent/panel-gap detection on a downscaled 1024px
                    # JPEG, low detail is more than sufficient; saves ~$3.70
                    # of every $4 of monthly vision-input spend.
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                ]},
            ]
            try:
                result = await self._parse_to(
                    response_format=PerImageOutput,
                    messages=per_image_messages,
                    max_tokens=_VISION_PER_IMAGE_MAX_TOKENS,
                    kind="vision_per_image",
                    lot_id=payload.lot_id,
                )
                per_image_results.append(result)
            except Exception:
                # Per-image failures are non-fatal: partial coverage is better
                # than failing the entire vision pass when one image is corrupt
                # or refused by the model. Aggregation runs on remaining results.
                log.exception(
                    "vision per-image failed",
                    photo_path=photo_path,
                    lot_id=payload.lot_id,
                )
                continue

        findings_json = [r.model_dump() for r in per_image_results]
        agg_user = (
            f"Per-image findings (JSON):\n{findings_json}\n\n"
            f"Description condition (claimed): {condition_str}\n"
            f"Description red flags: {red_flags_str}\n"
            f"Description green flags: {green_flags_str}"
        )
        agg_messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": VISION_AGGREGATION_PROMPT},
            {"role": "user", "content": agg_user},
        ]
        # Aggregation failure propagates — the worker decides how to handle it.
        return await self._parse_to(
            response_format=VisionOutput,
            messages=agg_messages,
            max_tokens=_VISION_AGGREGATE_MAX_TOKENS,
            kind="vision_aggregate",
            lot_id=payload.lot_id,
        )
