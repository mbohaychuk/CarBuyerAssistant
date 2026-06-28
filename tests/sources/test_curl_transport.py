"""The curl_cffi httpx transport adapter — translation + impersonation/proxy
threading, exercised with an injected fake session (no libcurl, no network)."""
from __future__ import annotations

from typing import Any

import httpx

from carbuyer.sources.curl_transport import CurlCffiTransport


class _FakeResp:
    def __init__(self) -> None:
        self.status_code = 200
        self.headers = {"content-type": "text/html"}
        self.content = b"<html>ok</html>"


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kw: Any) -> _FakeResp:
        self.calls.append((method, url, kw))
        return _FakeResp()

    async def close(self) -> None:
        self.closed = True


async def test_translates_response_and_threads_impersonate_and_proxy() -> None:
    fake = _FakeSession()
    transport = CurlCffiTransport(impersonate="chrome124", proxy="http://p:1", session=fake)

    resp = await transport.handle_async_request(
        httpx.Request("GET", "https://x/y", headers={"x-test": "1"}),
    )

    assert resp.status_code == 200  # noqa: PLR2004 -- the fake's fixed status
    assert resp.content == b"<html>ok</html>"
    method, url, kw = fake.calls[0]
    assert method == "GET"
    assert url == "https://x/y"
    assert kw["impersonate"] == "chrome124"
    assert kw["proxies"] == {"http": "http://p:1", "https": "http://p:1"}
    assert kw["allow_redirects"] is False  # httpx follows redirects above the transport


async def test_no_proxy_passes_none() -> None:
    fake = _FakeSession()
    transport = CurlCffiTransport(impersonate="chrome", session=fake)
    await transport.handle_async_request(httpx.Request("GET", "https://x"))
    assert fake.calls[0][2]["proxies"] is None


async def test_aclose_closes_session() -> None:
    fake = _FakeSession()
    transport = CurlCffiTransport(impersonate="chrome", session=fake)
    await transport.aclose()
    assert fake.closed is True
