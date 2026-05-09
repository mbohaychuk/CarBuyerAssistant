from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import ClassVar, Self

import httpx

from carbuyer.sources.base import (
    AuctionRef,
    AuctionSource,
    BidObservation,
    LotRef,
    RawAuction,
    RawLot,
    register,
)
from carbuyer.sources.hibid.parser import (
    HibidLotSummary,
    extract_lot_models,
    parse_lot_summary,
    raw_lot_id,
)
from carbuyer.sources.hibid.urls import catalog_url, lot_url, province_vehicles_url
from carbuyer.sources.http import jittered_sleep, make_client
from carbuyer.sources.retry import RetryTransport


class HibidSource(AuctionSource):
    name: ClassVar[str] = "hibid"
    # Bump when parse_lot_summary or discover/fetch contracts change.
    version: ClassVar[str] = "1"

    def __init__(
        self,
        provinces: list[str],
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provinces = provinces
        # Tests inject a MockTransport; production wires a RetryTransport
        # around an httpx.AsyncHTTPTransport in __aenter__.
        self._injected_transport = _transport
        self._client_cm: AbstractAsyncContextManager[httpx.AsyncClient] | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        transport = self._injected_transport or RetryTransport(
            httpx.AsyncHTTPTransport(),
        )
        self._client_cm = make_client(transport=transport)
        self._client = await self._client_cm.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client_cm is not None:
            await self._client_cm.__aexit__(exc_type, exc, tb)
        self._client_cm = None
        self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "HibidSource used outside `async with` — wrap in context manager",
            )
        return self._client

    async def discover_auctions(self) -> AsyncIterator[AuctionRef]:
        seen: set[str] = set()
        for i, province in enumerate(self.provinces):
            url = province_vehicles_url(province)
            resp = await self._http.get(url)
            resp.raise_for_status()
            for raw in extract_lot_models(resp.text):
                summary = parse_lot_summary(raw)
                auction_id = summary.auction_external_id
                if not auction_id or auction_id in seen:
                    continue
                seen.add(auction_id)
                yield AuctionRef(
                    source="hibid",
                    source_auction_id=auction_id,
                    url=catalog_url(auction_id),
                )
            if i < len(self.provinces) - 1:
                await jittered_sleep()

    async def fetch_auction(self, ref: AuctionRef) -> RawAuction:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        # The catalog page contains both auction-level metadata (in the page
        # header) and lotModels. For MVP, we record minimum metadata; richer
        # extraction (BP, terms_text) is left to a follow-up.
        return RawAuction(
            ref=ref,
            title=None,
            description=None,
            auctioneer_name=None,
            auctioneer_external_id=None,
            scheduled_start_at=None,
            scheduled_end_at=None,
            pickup_address=None,
            pickup_city=None,
            pickup_province=None,
            pickup_window_text=None,
            buyer_premium_pct=Decimal("0.10"),  # conservative default
            online_bidding_fee_pct=None,
            terms_text=None,
            auction_subtype="estate",
        )

    async def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        for raw in extract_lot_models(resp.text):
            summary = parse_lot_summary(raw)
            if not summary.source_lot_id:
                continue
            yield LotRef(
                source="hibid",
                source_auction_id=ref.source_auction_id,
                source_lot_id=summary.source_lot_id,
                url=summary.url or lot_url(summary.source_lot_id),
            )

    async def fetch_lot(self, ref: LotRef) -> RawLot:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        target = self._find_summary(resp.text, ref.source_lot_id)
        if target is None:
            raise ValueError(f"lot {ref.source_lot_id} not found at {ref.url}")
        return RawLot(
            ref=ref,
            lot_number=target.lot_number,
            title=target.title,
            description=target.description,
            photos=target.photos,
            year=target.year,
            make=target.make,
            model=target.model,
            current_high_bid_cad=target.current_high_bid_cad,
            bid_count_visible=target.bid_count_visible,
            scheduled_end_at=target.end_at,
            lot_status="open",
            extra=target.extra,
        )

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        target = self._find_summary(resp.text, ref.source_lot_id)
        if target is None:
            return BidObservation(
                ref=ref,
                observed_at=datetime.now(UTC),
                current_high_bid_cad=None,
                end_time_at_observation=None,
                status_at_observation="missing",
            )
        return BidObservation(
            ref=ref,
            observed_at=datetime.now(UTC),
            current_high_bid_cad=target.current_high_bid_cad,
            end_time_at_observation=target.end_at,
            status_at_observation="open",
        )

    @staticmethod
    def _find_summary(html: str, source_lot_id: str) -> HibidLotSummary | None:
        for raw in extract_lot_models(html):
            if raw_lot_id(raw) == source_lot_id:
                return parse_lot_summary(raw)
        return None


# Register at import time so the lot-scraper / discoverer worker / dashboard
# health view can enumerate covered platforms via SOURCES (Phase 0 design #11).
# Provinces default to AB/BC/SK/MB; phase-2 workers can re-instantiate with a
# different list and call register() again with the same name to override.
register(HibidSource(provinces=["AB", "BC", "SK", "MB"]))
