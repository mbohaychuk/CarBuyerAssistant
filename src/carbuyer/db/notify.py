from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator

import psycopg
from psycopg import sql as psql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

# Channel names: lowercase identifier rules; allowlist enforced at every callsite
# because LISTEN doesn't accept parameter binding (channel name is a SQL identifier).
CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Postgres NOTIFY hard limit is 8000 bytes; leave headroom for protocol overhead.
_MAX_PAYLOAD_BYTES = 7900


log = get_logger("db.notify")


def _check_channel(channel: str) -> None:
    if not CHANNEL_RE.match(channel):
        raise ValueError(f"invalid channel name: {channel!r}")


def to_psycopg_url(sa_url: str) -> str:
    """SQLAlchemy URL → psycopg URL (drops the +driver suffix)."""
    for prefix in (
        "postgresql+psycopg://",
        "postgresql+asyncpg://",
        "postgresql+psycopg2://",
    ):
        if sa_url.startswith(prefix):
            return "postgresql://" + sa_url[len(prefix):]
    if sa_url.startswith("postgresql://"):
        return sa_url
    raise ValueError(f"unsupported database URL scheme: {sa_url[:30]}...")


async def notify(session: AsyncSession, channel: str, payload: str = "") -> None:
    """Send a NOTIFY using ``pg_notify()`` so both args are properly bound.

    Channel name is validated against an allowlist regex; payload is capped at
    7900 bytes (Postgres' NOTIFY limit is 8000). The NOTIFY is queued at session
    level and dispatched at outer commit — call this near the end of the
    transaction that produced the work the listener will consume.
    """
    _check_channel(channel)
    if len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"NOTIFY payload exceeds {_MAX_PAYLOAD_BYTES} bytes",
        )
    await session.execute(
        text("SELECT pg_notify(:c, :p)"),
        {"c": channel, "p": payload},
    )


async def listen(
    channel: str,
    *,
    url: str | None = None,
    reconnect_max_backoff: float = 30.0,
) -> AsyncGenerator[str, None]:
    """Yield NOTIFY payloads on ``channel``, reconnecting on connection drops.

    The connection is autocommit and deliberately has no statement_timeout
    (LISTEN must block). On psycopg.OperationalError or OSError, the loop
    sleeps with exponential backoff and reconnects. Callers should run a
    catchup-pending sweep on startup to recover NOTIFYs missed during downtime.
    """
    _check_channel(channel)
    psycopg_url = to_psycopg_url(url or settings.database_url)
    backoff = 1.0
    while True:
        try:
            aconn = await psycopg.AsyncConnection.connect(
                psycopg_url, autocommit=True,
            )
            try:
                async with aconn.cursor() as cur:
                    # Identifier-quote the channel via psycopg.sql.SQL.format
                    # for safe interpolation (channel was already validated).
                    await cur.execute(
                        psql.SQL("LISTEN {ch}").format(ch=psql.Identifier(channel)),
                    )
                backoff = 1.0
                async for n in aconn.notifies():
                    yield n.payload
            finally:
                await aconn.close()
        except (psycopg.OperationalError, OSError) as exc:
            log.warning(
                "listen reconnect",
                channel=channel,
                error=str(exc),
                backoff=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, reconnect_max_backoff)
