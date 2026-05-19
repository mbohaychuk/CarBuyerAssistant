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
    # HiBid-specific: the per-listing row id used by HiBid's eventItemIds
    # filter. Other sources leave this None. See db.models.AuctionLot's
    # source_lot_row_id column for the full rationale.
    source_lot_row_id: int | None = None


@dataclass(slots=True)
class RawAuction:
    """One auction's worth of parsed metadata, ready to upsert.

    Plugin author contract — invariants the upsert and downstream workers
    rely on but that the type system can't enforce:

    1. **All datetime fields must be UTC-aware.** `scheduled_start_at` and
       `scheduled_end_at` flow into `bid_poller`'s priority-queue ordering and
       the closing-soon trigger evaluator, which both do timezone-aware
       arithmetic. A naive datetime raises `TypeError` mid-comparison. Construct
       via `datetime.fromisoformat(...).replace(tzinfo=UTC)` or equivalent.

    2. **`ref.source_auction_id` must be stable across re-ingestion** — it's
       half of the `(source, source_auction_id)` upsert key. Use a permanent ID
       from the source (HiBid's catalog id, McDougall's GUID), never a
       per-session number that rotates per ingest.

    3. **`buyer_premium_pct` is a decimal fraction**, not a percentage.
       0.10 means 10%. If the source quotes "15%", emit `Decimal("0.15")`.
       Leaving this `None` makes `all_in_cost` compute a 0% premium and
       silently mis-prices every lot from this source.
    """

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
    # Linear premium percent. Cap/floor live at end-of-class (see
    # buyer_premium_max_cad / buyer_premium_min_cad) because dataclass
    # ordering forbids defaulted fields before non-defaulted ones.
    buyer_premium_pct: Decimal | None
    online_bidding_fee_pct: Decimal | None
    terms_text: str | None
    auction_subtype: str = "estate"
    # Source-specific fields that don't yet warrant a canonical column.
    # Promote to a real field once 2+ sources surface the same key.
    extra: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    # Premium-amount cap/floor; NULL = unconstrained (HiBid). McDougall sets
    # max=2000 min=20 ("15% to a Max $2000, Min $20"). Clamped against the
    # linear `bid * buyer_premium_pct` in scoring.score.all_in_cost.
    buyer_premium_max_cad: Decimal | None = None
    buyer_premium_min_cad: Decimal | None = None


@dataclass(slots=True)
class RawLot:
    """One lot's worth of parsed fields, ready to upsert.

    Plugin author contract — same shape as RawAuction's, narrowed to lot-level:

    1. **`scheduled_end_at` must be UTC-aware when set.** It's coalesced with
       `auction.scheduled_end_at` in `bid_poller._load_open_lot_refs` for
       polling priority; a naive datetime crashes the sort. `None` is fine
       (the auction-level end time is used as fallback).

    2. **`ref.source_lot_id` must be stable across re-ingestion** — it's half
       of the `(auction_id, source_lot_id)` upsert key. A session-scoped numeric
       ID that changes per scrape silently duplicates lots every ingest cycle.

    3. **`lot_status` permitted values: `"open"`, `"closed"`, `"missing"`.**
       Maps to `carbuyer.db.enums.LotStatus` at upsert. Other strings will round-
       trip into the DB column but downstream filters (which use enum members)
       won't match them. New states require a `LotStatus` enum member first.

    4. **`year`, `make`, `model`, `vin` are written only on INSERT** by the
       upsert helper — they're considered "raw" inputs the enricher later
       normalizes. A rescrape with `make="ford"` won't clobber an enricher-set
       `make="Ford"`. This means cleaning these fields post-hoc requires a
       separate UPDATE, not just a re-ingest.
    """

    ref: LotRef
    lot_number: str | None
    title: str | None
    description: str | None
    photos: list[str] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    # See LotRef.source_lot_row_id. Plumbed through so the upsert helper
    # populates AuctionLot.source_lot_row_id without going through ref.
    source_lot_row_id: int | None = None
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
# Plugins call `register(self)` at module import time; the ingester / dashboard
# read SOURCES to enumerate covered platforms (used by the "needs-plugin"
# alerting and the dashboard health view).

SOURCES: dict[str, Source] = {}


def register(source: Source) -> None:
    SOURCES[source.name] = source
