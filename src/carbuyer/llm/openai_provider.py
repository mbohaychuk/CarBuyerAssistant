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

import time
from types import TracebackType
from typing import Self, TypeVar

from openai import APIError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel

from carbuyer.llm.base import (
    DescribeInput,
    LLMProvider,
    VisionInput,
)
from carbuyer.llm.prompts import description_system_prompt, description_user_prompt
from carbuyer.llm.schemas import EnrichmentOutput, VisionOutput
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("openai_provider")

# Phase 3 design overlay #10: 3000-token output ceiling. Worst-case
# EnrichmentOutput with multiple flag entries carrying verbatim evidence quotes
# tokenizes around 1.5-2k completion tokens; 2048 had no margin and truncation
# produces invalid JSON which the schema rejects, marking the lot failed.
DESCRIBE_MAX_TOKENS = 3000

T = TypeVar("T", bound=BaseModel)


class OpenAIProvider(LLMProvider):
    """OpenAI implementation. Implements both `describe` and `vision`
    (Phase 8 fills in the vision pass — for now it raises NotImplementedError).
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
        system: str,
        user: str,
        max_tokens: int,
        kind: str,
        lot_id: int | None,
    ) -> T:
        """Single chokepoint for the OpenAI structured-output call shape.

        Logs token usage + duration on every call. Phase 8 vision pass reuses
        this helper.
        """
        t0 = time.monotonic()
        try:
            response = await self.client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_format,
                temperature=0,
                max_tokens=max_tokens,
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
        return await self._parse_to(
            response_format=EnrichmentOutput,
            system=self._describe_system_prompt,
            user=user_prompt,
            max_tokens=DESCRIBE_MAX_TOKENS,
            kind="describe",
            lot_id=payload.lot_id,
        )

    async def vision(self, payload: VisionInput) -> VisionOutput:
        del payload  # vision pass implemented in Phase 8 — payload unused here.
        msg = "vision pass implemented in Phase 8"
        raise NotImplementedError(msg)
