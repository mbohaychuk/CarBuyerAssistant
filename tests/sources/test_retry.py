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


@pytest.mark.asyncio
async def test_retry_transport_caps_huge_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3600s Retry-After must not stall the worker for an hour."""
    import carbuyer.sources.retry as retry_mod

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(retry_mod.asyncio, "sleep", fake_sleep)

    call_count = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        max_failures = 2
        if call_count["n"] <= max_failures:
            return httpx.Response(
                HTTP_SERVICE_UNAVAILABLE, headers={"Retry-After": "3600"},
            )
        return httpx.Response(HTTP_OK, text="ok")

    transport = RetryTransport(
        httpx.MockTransport(handler),
        max_retries=4, base=0.01, cap=0.05, retry_after_cap=2.0,
    )
    async with make_client(transport=transport) as client:
        r = await client.get("https://example.test/")
    assert r.status_code == HTTP_OK
    # Both retry sleeps must be ≤ retry_after_cap + jitter (25%).
    assert all(s <= 2.0 * 1.26 for s in sleeps), sleeps  # noqa: PLR2004
