"""curl_cffi-backed httpx transport for TLS/JA3 impersonation.

Anti-bot sites (Kijiji et al.) fingerprint the TLS handshake — JA3, the H2
SETTINGS frame order — not just the User-Agent. curl_cffi replays a real
browser's handshake; this adapter exposes it as an ``httpx.AsyncBaseTransport``
so it slots straight into ``make_client(transport=)`` under ``RetryTransport``,
with the rest of the source stack (retry, redirects, parsing) unchanged.

Redirects are handled by httpx *above* the transport, so this issues single,
non-redirecting requests and returns the raw response.
"""
from __future__ import annotations

from typing import Any, Protocol, cast

import httpx
from curl_cffi.requests import AsyncSession


class _CurlResponse(Protocol):
    status_code: int
    headers: Any
    content: bytes


class _CurlSession(Protocol):
    async def request(self, method: str, url: str, **kwargs: Any) -> _CurlResponse: ...
    async def close(self) -> None: ...


class CurlCffiTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        impersonate: str,
        proxy: str | None = None,
        session: _CurlSession | None = None,
    ) -> None:
        self._impersonate = impersonate
        self._proxy = proxy
        # Injectable for tests; the real session is constructed lazily (libcurl
        # handle allocation) on first request, not at import time.
        self._session: _CurlSession | None = session

    def _get_session(self) -> _CurlSession:
        session = self._session
        if session is None:
            session = cast("_CurlSession", AsyncSession())
            self._session = session
        return session

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        session = self._get_session()
        proxies = (
            {"http": self._proxy, "https": self._proxy} if self._proxy else None
        )
        resp = await session.request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            data=request.content or None,
            impersonate=self._impersonate,
            proxies=proxies,
            allow_redirects=False,  # httpx follows redirects above the transport
        )
        raw_headers = resp.headers
        items = (
            list(raw_headers.multi_items())
            if hasattr(raw_headers, "multi_items")
            else list(dict(raw_headers).items())
        )
        # curl_cffi already returns the decoded body, so drop the headers that
        # describe the *encoded* upstream body — otherwise httpx re-decompresses
        # it ("incorrect header check") and Content-Length mismatches the body.
        headers = [
            (k, v) for k, v in items if k.lower() not in ("content-encoding", "content-length")
        ]
        return httpx.Response(
            status_code=resp.status_code,
            headers=headers,
            content=resp.content,
            request=request,
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
