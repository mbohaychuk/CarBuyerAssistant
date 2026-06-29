"""LLM provider abstractions.

Phase 3 design overlay #17: split into two role mixins (`DescribeProvider`,
`VisionProvider`) symmetric with `AuctionDiscoverer` / `AuctionFetcher` /
`BidPoller` in `sources/base.py`. A full-capability provider (OpenAI,
Anthropic) implements both via the `LLMProvider` union; a describe-only local
model implements only `DescribeProvider`.

Both ABCs ship `__aenter__` / `__aexit__` defaults so workers can manage the
provider's HTTP client lifecycle via `async with` (closes the underlying
`AsyncOpenAI` client on shutdown).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

from carbuyer.llm.schemas import ArchetypeExpansion, EnrichmentOutput, VisionOutput


@dataclass(slots=True)
class DescribeInput:
    """Input to LLM description pass — everything the model needs to reason
    about a single lot. Includes auction-context (subtype, pickup province),
    bidding state (high bid, increment, close time, reserve), and listing
    completeness signals (image count) per Phase 3 design overlay #24.
    """
    lot_id: int
    title: str
    description: str
    year: int | None
    make: str | None
    model: str | None
    auctioneer_name: str | None
    auction_subtype: str
    pickup_province: str | None
    raw_carfax_url: str | None
    current_high_bid_cad: Decimal | None
    bid_increment: Decimal | None
    auction_close_at: datetime | None
    is_no_reserve: bool
    image_count: int
    current_year: int


@dataclass(slots=True)
class VisionInput:
    # lot_id is forwarded into per-call usage logs so cost/latency lines stay
    # correlatable to a specific lot in production. None is allowed for
    # standalone evaluations / smoke tests that aren't tied to a DB row.
    lot_id: int | None
    photo_paths: list[str]
    year: int | None
    make: str | None
    model: str | None
    description_condition: str | None
    description_red_flags: list[str]
    description_green_flags: list[str]


class _AsyncCM:
    """Mixin: default no-op async-context-manager. Concrete providers override
    `__aexit__` to close their HTTP client."""
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None


class DescribeProvider(_AsyncCM, ABC):
    name: str = "abstract"

    @abstractmethod
    async def describe(self, payload: DescribeInput) -> EnrichmentOutput: ...


class VisionProvider(_AsyncCM, ABC):
    name: str = "abstract"

    @abstractmethod
    async def vision(self, payload: VisionInput) -> VisionOutput: ...


class ArchetypeProvider(_AsyncCM, ABC):
    name: str = "abstract"

    @abstractmethod
    async def expand_archetype(self, text: str) -> ArchetypeExpansion: ...


class LLMProvider(DescribeProvider, VisionProvider, ABC):
    """Convenience union for providers that implement both roles (OpenAI,
    Anthropic). Use the role ABCs directly when a worker needs only one."""
