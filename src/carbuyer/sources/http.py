from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx

from carbuyer.shared.config import settings

# HTTP/2 is disabled by default: HTTP/2 fingerprinting (h2c settings frame
# ordering, etc.) is more revealing than UA, and httpx's H2 backend requires
# the `httpx[http2]` extra which we don't depend on.
#
# Phase 1 wraps this with a RetryTransport that honors Retry-After and applies
# jittered exponential backoff on {429, 502, 503, 504}. The `transport`
# parameter is the seam.


def build_default_headers() -> dict[str, str]:
    return {
        "User-Agent": settings.http_user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }


@asynccontextmanager
async def make_client(
    *,
    timeout: float = 30.0,  # noqa: ASYNC109  -- this is the httpx timeout, not asyncio.timeout
    follow_redirects: bool = True,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    merged_headers = build_default_headers()
    if headers:
        merged_headers.update(headers)
    async with httpx.AsyncClient(
        headers=merged_headers,
        timeout=timeout,
        follow_redirects=follow_redirects,
        http2=False,
        transport=transport,
    ) as client:
        yield client


async def jittered_sleep(min_s: float = 4.0, max_s: float = 8.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))
