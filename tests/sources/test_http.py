from importlib import reload

import pytest
import respx

import carbuyer.shared.config as config_mod
import carbuyer.sources.http as http_mod
from carbuyer.sources.http import make_client

HTTP_OK = 200


@pytest.mark.asyncio
async def test_make_client_sends_browser_headers() -> None:
    async with respx.mock(base_url="https://example.test") as mock:
        route = mock.get("/").respond(HTTP_OK, text="ok")
        async with make_client() as client:
            r = await client.get("https://example.test/")
        assert r.status_code == HTTP_OK
        sent = route.calls.last.request
        assert "Mozilla" in sent.headers["User-Agent"]
        assert "Accept-Language" in sent.headers


@pytest.mark.asyncio
async def test_make_client_merges_custom_headers() -> None:
    async with respx.mock(base_url="https://example.test") as mock:
        route = mock.get("/").respond(HTTP_OK)
        async with make_client(headers={"X-Plugin": "hibid"}) as client:
            await client.get("https://example.test/")
        sent = route.calls.last.request
        assert sent.headers["X-Plugin"] == "hibid"
        assert "Mozilla" in sent.headers["User-Agent"]


def test_build_default_headers_uses_settings_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_USER_AGENT", "TestUA/1.0")
    # Reload settings to pick up the env var override; reload http to rebind
    # its `settings` reference to the freshly-loaded module.
    reload(config_mod)
    reload(http_mod)
    headers = http_mod.build_default_headers()
    assert headers["User-Agent"] == "TestUA/1.0"
