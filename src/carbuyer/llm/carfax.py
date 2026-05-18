"""Carfax URL extraction + best-effort fetch / LLM extraction.

Phase 3 design overlay #20 split this into two reliability tiers:

- ``find_carfax_url`` (regex on the listing description) — reliable, ships,
  used by the enricher unconditionally.

- ``fetch_carfax_text`` + ``extract_carfax_findings`` — best-effort. Carfax
  CA / .com is paywalled and Cloudflare-bot-detected. Plain ``httpx.get`` will
  return a login wall, a 403, or under the Phase 1 idiom for HiBid blocking.
  Expect <30% success rate on real auction-listing Carfax URLs. The HTTP gate
  (status>=400 OR body<{CARFAX_MIN_HTML_BYTES} bytes) drops obvious failures
  before paying for an LLM call. Phase 8 vision-batcher (Playwright) is the
  longer-term home for full Carfax extraction.

Phase 3 design overlay #18: ``extract_carfax_findings`` accepts the caller's
AsyncOpenAI client + model — does not construct its own. Avoids per-lot TLS
handshake and duplicate retry/timeout config.

Phase 3 design overlay #22: ``redact_carfax_url`` strips the path token from
log lines. The token is a per-vehicle access key; treat as PII-adjacent.
"""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from carbuyer.llm.schemas import CarfaxFindings
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.sources.http import build_default_headers

if TYPE_CHECKING:
    from openai import AsyncOpenAI

log = get_logger("carfax")

_CARFAX_HOSTS = ("carfax.ca", "www.carfax.ca", "carfax.com", "www.carfax.com")
_CARFAX_PATTERN = re.compile(r"https?://[\w.-]*carfax\.(ca|com)/\S+", re.IGNORECASE)

# Below this byte count the response is almost certainly a login wall, a 403
# page, or a CDN block — not a real report. Skip the LLM call.
CARFAX_MIN_HTML_BYTES = 500
CARFAX_FETCH_TIMEOUT_S = 15.0
CARFAX_HTML_TRUNCATE = 8000
CARFAX_MAX_TOKENS = 512
HTTP_BAD_REQUEST = 400


def find_carfax_url(text: str | None) -> str | None:
    """Regex out a Carfax CA / .com URL from listing description text.

    Strips trailing punctuation that breaks `\\S+`. Verifies the hostname is
    actually carfax.{ca,com} (not e.g. carfax.evil.com).
    """
    if not text:
        return None
    match = _CARFAX_PATTERN.search(text)
    if match is None:
        return None
    url = match.group(0).rstrip(".,);")
    if urlparse(url).hostname not in _CARFAX_HOSTS:
        return None
    return url


def redact_carfax_url(url: str | None) -> str:
    """Return a log-safe representation of a Carfax URL.

    The path segment of a Carfax report URL is a per-vehicle access token —
    don't log it. Returns ``carfax.ca?h=<sha256-prefix>`` so multiple log lines
    for the same URL group together without exposing the token.
    """
    if not url:
        return "(none)"
    host = urlparse(url).hostname or "unknown"
    h = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{host}?h={h}"


async def fetch_carfax_text(
    url: str,
    *,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """Best-effort fetch a Carfax report's HTML.

    Returns ``None`` on any of: 4xx/5xx response, body smaller than
    ``CARFAX_MIN_HTML_BYTES``, network error, timeout. The caller treats
    ``None`` as the common case (Carfax is paywalled / bot-blocked).
    """
    log_url = redact_carfax_url(url)
    try:
        client = httpx.AsyncClient(
            transport=_transport,
            headers=build_default_headers(),
            timeout=CARFAX_FETCH_TIMEOUT_S,
            follow_redirects=True,
        )
        async with client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        log.info(
            "carfax fetch failed (network)",
            url=log_url, exc=type(exc).__name__,
        )
        return None
    if response.status_code >= HTTP_BAD_REQUEST:
        log.info(
            "carfax fetch failed (status)",
            url=log_url, status=response.status_code,
        )
        return None
    text = response.text
    if len(text) < CARFAX_MIN_HTML_BYTES:
        log.info(
            "carfax fetch failed (short body — likely paywall)",
            url=log_url, byte_len=len(text),
        )
        return None
    return text


async def extract_carfax_findings(
    html: str,
    *,
    client: AsyncOpenAI,
    model: str | None = None,
) -> CarfaxFindings | None:
    """Best-effort LLM extraction of structured Carfax findings.

    Strips HTML tags and collapses whitespace, then truncates to
    ``CARFAX_HTML_TRUNCATE`` chars. Returns ``None`` on any LLM failure —
    Carfax is supplementary signal, not gating.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)[:CARFAX_HTML_TRUNCATE]
    use_model = model or settings.openai_model
    try:
        from carbuyer.llm.openai_provider import _is_reasoning_model  # noqa: PLC0415

        extra: dict[str, object] = {}
        if _is_reasoning_model(use_model):
            extra["max_completion_tokens"] = CARFAX_MAX_TOKENS
            if settings.openai_reasoning_effort:
                extra["reasoning_effort"] = settings.openai_reasoning_effort
        else:
            extra["max_tokens"] = CARFAX_MAX_TOKENS
            extra["temperature"] = 0
        response = await client.chat.completions.parse(
            model=use_model,
            messages=[
                {"role": "system", "content": (
                    "Extract structured Carfax findings from the report text. "
                    "Output `unknown` only when the field allows it; never "
                    "invent facts; do not paraphrase brands."
                )},
                {"role": "user", "content": text},
            ],
            response_format=CarfaxFindings,
            **extra,  # type: ignore[arg-type]
        )
    except Exception:
        log.exception("carfax extract failed")
        return None
    return response.choices[0].message.parsed
