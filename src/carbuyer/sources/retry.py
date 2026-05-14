from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from carbuyer.shared.logging import get_logger

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

# A misbehaving server (or a CDN incident) can send Retry-After: 3600. With
# max_retries=4, an unbounded honor would stall a worker process for hours.
# Cap independently of `cap` (which only bounds exponential backoff) so server
# guidance is respected but bounded.
DEFAULT_RETRY_AFTER_CAP_S = 120.0

log = get_logger("sources.retry")


def _parse_retry_after(value: str) -> float | None:
    # RFC 7231: Retry-After is either delta-seconds (int) or HTTP-date.
    s = value.strip()
    if not s:
        return None
    if s.isdigit():
        return float(s)
    try:
        when = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class RetryTransport(httpx.AsyncBaseTransport):
    """Wrap an inner transport; retry transient errors with backoff.

    Honors Retry-After when present; otherwise uses jittered exponential
    backoff capped at `cap` seconds. Status codes outside RETRYABLE_STATUS
    are returned to the caller without retry.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        max_retries: int = 4,
        base: float = 1.0,
        cap: float = 30.0,
        retry_after_cap: float = DEFAULT_RETRY_AFTER_CAP_S,
    ) -> None:
        self._inner = inner
        self._max_retries = max_retries
        self._base = base
        self._cap = cap
        self._retry_after_cap = retry_after_cap

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            response = await self._inner.handle_async_request(request)
            if response.status_code not in RETRYABLE_STATUS:
                return response
            last = response
            if attempt == self._max_retries:
                return response
            # Drain and close before retrying so the connection can be reused.
            await response.aread()
            await response.aclose()
            ra = response.headers.get("Retry-After")
            delay = _parse_retry_after(ra) if ra else None
            if delay is None:
                delay = min(self._cap, self._base * (2**attempt))
            elif delay > self._retry_after_cap:
                log.warning(
                    "retry-after capped",
                    url=str(request.url),
                    server_value_s=delay,
                    cap_s=self._retry_after_cap,
                )
                delay = self._retry_after_cap
            delay = delay + random.uniform(0, max(delay * 0.25, 0.05))  # jitter
            await asyncio.sleep(delay)
        # Unreachable, but keeps the type checker happy.
        assert last is not None
        return last

    async def aclose(self) -> None:
        await self._inner.aclose()
