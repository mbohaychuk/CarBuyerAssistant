import httpx
import pytest

from carbuyer.sources.http import make_client
from carbuyer.sources.retry import RetryTransport

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_SERVICE_UNAVAILABLE = 503


@pytest.mark.asyncio
async def test_retry_transport_retries_503_then_succeeds() -> None:
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        max_failures = 2
        if call_count["n"] <= max_failures:
            return httpx.Response(HTTP_SERVICE_UNAVAILABLE, headers={"Retry-After": "0"})
        return httpx.Response(HTTP_OK, text="ok")

    inner = httpx.MockTransport(handler)
    transport = RetryTransport(inner, max_retries=4, base=0.01, cap=0.05)
    async with make_client(transport=transport) as client:
        r = await client.get("https://example.test/")
    assert r.status_code == HTTP_OK
    expected_calls = 3
    assert call_count["n"] == expected_calls


@pytest.mark.asyncio
async def test_retry_transport_gives_up_after_max() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTP_SERVICE_UNAVAILABLE, headers={"Retry-After": "0"})

    transport = RetryTransport(
        httpx.MockTransport(handler), max_retries=2, base=0.01, cap=0.05,
    )
    async with make_client(transport=transport) as client:
        r = await client.get("https://example.test/")
    # Exhausted retries; final response returned.
    assert r.status_code == HTTP_SERVICE_UNAVAILABLE


@pytest.mark.asyncio
async def test_retry_transport_does_not_retry_404() -> None:
    call_count = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(HTTP_NOT_FOUND)

    transport = RetryTransport(
        httpx.MockTransport(handler), max_retries=4, base=0.01, cap=0.05,
    )
    async with make_client(transport=transport) as client:
        r = await client.get("https://example.test/")
    assert r.status_code == HTTP_NOT_FOUND
    assert call_count["n"] == 1
