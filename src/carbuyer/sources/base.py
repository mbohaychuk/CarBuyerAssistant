from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, ClassVar, Literal, Self

SourceType = Literal["listing", "auction"]


# ── Reference / value objects ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuctionRef:
    source: str
    source_auction_id: str
    url: str


@dataclass(frozen=True, slots=True)
class LotRef:
    source: str
    source_auction_id: str
    source_lot_id: str
    url: str


@dataclass(slots=True)
class RawAuction:
    ref: AuctionRef
    title: str | None
    description: str | None
    auctioneer_name: str | None
    auctioneer_external_id: str | None
    scheduled_start_at: datetime | None
    scheduled_end_at: datetime | None
    pickup_address: str | None
    pickup_city: str | None
    pickup_province: str | None
    pickup_window_text: str | None
    buyer_premium_pct: Decimal | None
    online_bidding_fee_pct: Decimal | None
    terms_text: str | None
    auction_subtype: str = "estate"
    # Source-specific fields that don't yet warrant a canonical column.
    # Promote to a real field once 2+ sources surface the same key.
    extra: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]


@dataclass(slots=True)
class RawLot:
    ref: LotRef
    lot_number: str | None
    title: str | None
    description: str | None
    photos: list[str] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    mileage_km: int | None = None
    vin: str | None = None
    current_high_bid_cad: Decimal | None = None
    bid_count_visible: int | None = None
    reserve_met: bool | None = None
    scheduled_end_at: datetime | None = None
    lot_status: str = "open"
    # See RawAuction.extra. Common uses today: carfax_url, reserve_price_cad,
    # buy_now_price_cad, raw HTML for downstream parsers.
    extra: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]


@dataclass(slots=True)
class BidObservation:
    ref: LotRef
    observed_at: datetime
    current_high_bid_cad: Decimal | None
    end_time_at_observation: datetime | None
    status_at_observation: str  # See db.enums.LotStatus for canonical values.


# ── Plugin role ABCs ────────────────────────────────────────────────────────
# Roles split so that a router (Phase 10) can implement only AuctionDiscoverer
# while a full plugin (HiBid, McDougall) implements all three via AuctionSource.
#
# NOTE: abstract async-generator methods are declared `def f(...) -> AsyncIterator[T]`
# (not `async def`). Concrete implementations are async generators
# (`async def f(...) -> AsyncIterator[T]: yield ...`); pyright accepts that
# pairing under strict mode. `async def f() -> AsyncIterator[T]: ...` (with `...`
# body) would be a coroutine returning an iterator — wrong shape, runtime bug.


class Source(ABC):
    """Marker base for all sources. Subclasses must define `name` and `version`."""

    name: ClassVar[str]
    # Bumped when the parser/discovery contract changes. Persisted to
    # `auction_lots.parser_version` so the enricher / valuator can re-run on
    # rows scraped by a stale version.
    version: ClassVar[str]

    @classmethod
    def parse_auction_url(cls, url: str) -> AuctionRef | None:
        """Return AuctionRef if THIS source is authoritative for the URL, else None.

        Concrete plugins override when they own a URL space (e.g. HiBidSource
        recognizes ``https://hibid.com/.../catalog/{id}``). The resolver helper
        (``carbuyer.sources.resolver.resolve_auction_url``) walks all registered
        sources to find a match. The default returns None (plugin doesn't claim
        this URL).
        """
        del url  # default impl ignores
        return None

    async def __aenter__(self) -> Self:
        """Default no-op; HibidSource and other plugins with HTTP-client
        lifecycle override this to acquire resources."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Default no-op; subclasses override to release resources."""
        del exc_type, exc, tb


class AuctionDiscoverer(Source):
    kind: ClassVar[SourceType] = "auction"

    @abstractmethod
    def discover_auctions(self) -> AsyncIterator[AuctionRef]: ...


class AuctionFetcher(Source):
    kind: ClassVar[SourceType] = "auction"

    @abstractmethod
    async def fetch_auction(self, ref: AuctionRef) -> RawAuction: ...

    @abstractmethod
    def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]: ...

    @abstractmethod
    async def fetch_lot(self, ref: LotRef) -> RawLot: ...


class BidPoller(Source):
    kind: ClassVar[SourceType] = "auction"

    @abstractmethod
    async def poll_bid(self, ref: LotRef) -> BidObservation: ...


class AuctionSource(AuctionDiscoverer, AuctionFetcher, BidPoller):
    """Convenience union for plugins that implement all three auction roles."""


# ── Registry ────────────────────────────────────────────────────────────────
# Plugins call `register(self)` at module import time; the lot-scraper /
# discoverer / dashboard read SOURCES to enumerate covered platforms (used by
# the Phase-10 "needs-plugin" alerting and the dashboard health view).

SOURCES: dict[str, Source] = {}


def register(source: Source) -> None:
    SOURCES[source.name] = source
