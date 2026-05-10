from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from carbuyer.llm.carfax import (
    CARFAX_MIN_HTML_BYTES,
    extract_carfax_findings,
    fetch_carfax_text,
    find_carfax_url,
    redact_carfax_url,
)
from carbuyer.llm.schemas import CarfaxFindings


def test_find_carfax_url_extracts_canadian_link() -> None:
    text = "Clean carfax: https://www.carfax.ca/vhr/abc123 . Email me."
    assert find_carfax_url(text) == "https://www.carfax.ca/vhr/abc123"


def test_find_carfax_url_extracts_us_link() -> None:
    text = "Vehicle history: https://carfax.com/VehicleHistory/p/Report.cfx?id=xyz999"
    assert find_carfax_url(text) == "https://carfax.com/VehicleHistory/p/Report.cfx?id=xyz999"


def test_find_carfax_url_returns_none_when_absent() -> None:
    assert find_carfax_url("just a description") is None


def test_find_carfax_url_returns_none_for_empty() -> None:
    assert find_carfax_url("") is None
    assert find_carfax_url(None) is None  # type: ignore[arg-type]


def test_find_carfax_url_strips_trailing_punctuation() -> None:
    assert find_carfax_url(
        "see https://www.carfax.ca/vhr/abc123,",
    ) == "https://www.carfax.ca/vhr/abc123"


def test_find_carfax_url_rejects_non_carfax_domain() -> None:
    """Spoof check: the URL host must be carfax.{ca,com}."""
    assert find_carfax_url("https://carfax.example.com/vhr/abc") is None
    assert find_carfax_url("https://carfax.evil.com/r") is None


def test_redact_carfax_url_drops_path_and_query() -> None:
    """Phase 3 design overlay #22: full URL is a per-vehicle access token."""
    redacted = redact_carfax_url("https://www.carfax.ca/vhr/secrettoken123")
    assert "secrettoken123" not in redacted
    assert "carfax.ca" in redacted


def test_redact_carfax_url_handles_none() -> None:
    assert redact_carfax_url(None) == "(none)"


@pytest.mark.asyncio
async def test_fetch_carfax_text_returns_text_on_success() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>real Carfax body" + ("x" * 1000) + "</html>")

    transport = httpx.MockTransport(handler)
    text = await fetch_carfax_text(
        "https://www.carfax.ca/vhr/abc", _transport=transport,
    )
    assert text is not None
    assert "real Carfax body" in text


@pytest.mark.asyncio
async def test_fetch_carfax_text_returns_none_on_4xx() -> None:
    """Phase 3 overlay #21: HTTP gate. 404 page passed to LLM costs money
    and produces garbage."""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html>Not Found</html>")

    transport = httpx.MockTransport(handler)
    text = await fetch_carfax_text(
        "https://www.carfax.ca/vhr/abc", _transport=transport,
    )
    assert text is None


@pytest.mark.asyncio
async def test_fetch_carfax_text_returns_none_on_short_body() -> None:
    """Phase 3 overlay #21: short body is a login wall, not a real report."""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="login required")

    transport = httpx.MockTransport(handler)
    text = await fetch_carfax_text(
        "https://www.carfax.ca/vhr/abc", _transport=transport,
    )
    assert text is None


@pytest.mark.asyncio
async def test_fetch_carfax_text_returns_none_on_5xx() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="bot detection")

    transport = httpx.MockTransport(handler)
    text = await fetch_carfax_text(
        "https://www.carfax.ca/vhr/abc", _transport=transport,
    )
    assert text is None


@pytest.mark.asyncio
async def test_fetch_carfax_text_min_bytes_threshold() -> None:
    body = "x" * (CARFAX_MIN_HTML_BYTES - 1)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    text = await fetch_carfax_text(
        "https://www.carfax.ca/vhr/abc", _transport=transport,
    )
    assert text is None


@pytest.mark.asyncio
async def test_extract_carfax_findings_passes_caller_client() -> None:
    """Phase 3 design overlay #18: extractor uses caller's AsyncOpenAI
    client, doesn't construct its own."""
    expected = CarfaxFindings(
        accident_count=2, accident_severity_max="moderate",
        service_record_density="regular", ownership_count=2,
        title_brands=[], odometer_consistency="consistent",
    )
    fake_choice = MagicMock()
    fake_choice.message.parsed = expected
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = MagicMock(
        prompt_tokens=400, completion_tokens=80, total_tokens=480,
    )
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=fake_response)

    findings = await extract_carfax_findings(
        "<html>Accidents Reported: 2</html>",
        client=client, model="gpt-4o-mini",
    )
    assert findings is not None
    assert findings.accident_count == 2  # noqa: PLR2004 -- explicit fixture value


@pytest.mark.asyncio
async def test_extract_carfax_findings_returns_none_on_failure() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(side_effect=RuntimeError("boom"))
    findings = await extract_carfax_findings(
        "<html>x</html>", client=client, model="gpt-4o-mini",
    )
    assert findings is None
