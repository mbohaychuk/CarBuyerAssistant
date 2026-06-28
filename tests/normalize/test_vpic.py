"""NHTSA vPIC model normalization — exercised with a MockTransport (no network).
Best-effort: no-match and HTTP errors return the input model unchanged."""
from __future__ import annotations

import httpx

from carbuyer.normalize import vpic


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _models(*names: str) -> httpx.MockTransport:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Results": [{"Make_Name": "FORD", "Model_Name": n} for n in names]},
        )
    return httpx.MockTransport(handler)


async def test_snaps_to_canonical_spelling() -> None:
    async with _client(_models("F-150", "Ranger")) as client:
        out = await vpic.canonical_model("Ford", "F150", 2015, client=client)
    assert out == "F-150"


async def test_no_match_returns_input() -> None:
    async with _client(_models("F-150", "Ranger")) as client:
        out = await vpic.canonical_model("Ford", "Bronco", 2015, client=client)
    assert out == "Bronco"


async def test_http_error_returns_input() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)
    async with _client(httpx.MockTransport(handler)) as client:
        out = await vpic.canonical_model("Ford", "F150", 2015, client=client)
    assert out == "F150"


async def test_missing_year_or_fields_returns_input() -> None:
    async with _client(_models("F-150")) as client:
        assert await vpic.canonical_model("Ford", "F150", None, client=client) == "F150"
        assert await vpic.canonical_model(None, "F150", 2015, client=client) == "F150"
        assert await vpic.canonical_model("Ford", None, 2015, client=client) is None
