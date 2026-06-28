"""NHTSA recalls + complaints reliability fetcher — MockTransport, no network.
Best-effort: HTTP errors and missing fields return None."""
# ruff: noqa: PLR2004 -- expected recall/complaint counts are inherent to the asserts
from __future__ import annotations

import httpx

from carbuyer.normalize import nhtsa


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_fetches_recall_and_complaint_counts() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "recalls" in str(req.url):
            return httpx.Response(200, json={"Count": 3, "results": [{}, {}, {}]})
        return httpx.Response(200, json={"count": 47, "results": []})

    async with _client(httpx.MockTransport(handler)) as client:
        recalls, complaints = await nhtsa.fetch_reliability("Ford", "F-150", 2015, client=client)
    assert recalls == 3
    assert complaints == 47


async def test_falls_back_to_results_length_when_no_count_field() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{}, {}]})

    async with _client(httpx.MockTransport(handler)) as client:
        recalls, complaints = await nhtsa.fetch_reliability("Ford", "F-150", 2015, client=client)
    assert recalls == 2
    assert complaints == 2


async def test_http_error_returns_none() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client(httpx.MockTransport(handler)) as client:
        recalls, complaints = await nhtsa.fetch_reliability("Ford", "F-150", 2015, client=client)
    assert recalls is None
    assert complaints is None


async def test_missing_make_or_year_skips_fetch() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Count": 0, "results": []})

    async with _client(httpx.MockTransport(handler)) as client:
        assert await nhtsa.fetch_reliability(None, "F-150", 2015, client=client) == (None, None)
        assert await nhtsa.fetch_reliability("Ford", "F-150", None, client=client) == (None, None)
