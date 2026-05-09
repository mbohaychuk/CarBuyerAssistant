import asyncio
from unittest.mock import AsyncMock

import psycopg
import pytest

from carbuyer.db.notify import CHANNEL_RE, listen, notify, to_psycopg_url
from carbuyer.shared.config import settings


def _test_url() -> str:
    # Match conftest's logic — suffix slicing avoids replacing the username,
    # which is also "carbuyer".
    url = settings.database_url
    if url.endswith("/carbuyer"):
        return url[: -len("/carbuyer")] + "/carbuyer_test"
    if url.endswith("/carbuyer_test"):
        return url
    raise RuntimeError(f"unexpected DATABASE_URL for tests: {url}")


@pytest.mark.asyncio
async def test_notify_round_trip() -> None:
    """End-to-end NOTIFY round-trip with a real autocommit connection.

    The shared `session` fixture wraps tests in a savepoint that never commits,
    but NOTIFY only fires on actual commit. So this test uses two direct
    autocommit psycopg connections — one for LISTEN, one for sending the NOTIFY.
    """
    received: list[str] = []
    test_url = _test_url()

    async def reader() -> None:
        async for payload in listen("test_channel", url=test_url):
            received.append(payload)
            return

    task = asyncio.create_task(reader())
    # Give the listener time to connect and execute LISTEN before we NOTIFY.
    await asyncio.sleep(0.5)

    # Send the NOTIFY via a direct autocommit psycopg connection (mirrors
    # production: workers commit the txn that called notify() and the listener
    # fires immediately).
    psycopg_url = to_psycopg_url(test_url)
    aconn = await psycopg.AsyncConnection.connect(psycopg_url, autocommit=True)
    try:
        async with aconn.cursor() as cur:
            await cur.execute("SELECT pg_notify(%s, %s)", ("test_channel", "hello"))
    finally:
        await aconn.close()

    await asyncio.wait_for(task, timeout=2.0)
    assert received == ["hello"]


@pytest.mark.asyncio
async def test_notify_rejects_invalid_channel() -> None:
    """notify() validates the channel name without needing a real session."""
    fake_session = AsyncMock()
    with pytest.raises(ValueError, match="invalid channel name"):
        await notify(fake_session, "bad-channel!", "x")
    with pytest.raises(ValueError, match="invalid channel name"):
        await notify(fake_session, "1starts-with-digit", "x")
    with pytest.raises(ValueError, match="invalid channel name"):
        await notify(fake_session, "Auction_Pending", "x")
    fake_session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_notify_rejects_oversized_payload() -> None:
    fake_session = AsyncMock()
    too_big = 8000
    with pytest.raises(ValueError, match="payload"):
        await notify(fake_session, "test_channel", "x" * too_big)
    fake_session.execute.assert_not_called()


def test_channel_regex() -> None:
    assert CHANNEL_RE.match("auction_pending")
    assert CHANNEL_RE.match("a")
    assert not CHANNEL_RE.match("Auction_Pending")
    assert not CHANNEL_RE.match("auction-pending")
    assert not CHANNEL_RE.match("1auction")
    assert not CHANNEL_RE.match("")
