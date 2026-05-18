"""Tests for pg_advisory_lock-based worker singleton enforcement.

These hit the test database directly via raw psycopg — the contract under
test is "two psycopg sessions on the same DB can't both hold the same
hashtext-keyed advisory lock," which only makes sense against a real
Postgres. SQLAlchemy session fixtures aren't useful here because pooled
sessions don't preserve a single connection long enough.
"""
from __future__ import annotations

import psycopg
import pytest

from carbuyer.db.notify import to_psycopg_url
from carbuyer.shared.config import settings
from carbuyer.shared.singleton import acquire_singleton_lock


def _test_psycopg_url() -> str:
    """Same routing as conftest._test_url() — append _test to carbuyer DB."""
    url = settings.database_url
    if url.endswith("/carbuyer"):
        url = url[: -len("/carbuyer")] + "/carbuyer_test"
    return to_psycopg_url(url)


@pytest.mark.asyncio
async def test_acquire_returns_connection_when_lock_is_free() -> None:
    """Happy path: a free lock name returns a held connection, and the
    lock releases when the caller closes it (a fresh acquire on the same
    name then succeeds without contention)."""
    name = "test_singleton_free_path"
    url = _test_psycopg_url()

    conn = await acquire_singleton_lock(name, database_url=url)
    try:
        assert conn is not None
        assert not conn.closed
    finally:
        await conn.close()

    # Lock released with the connection → next acquire succeeds.
    conn2 = await acquire_singleton_lock(name, database_url=url)
    try:
        assert not conn2.closed
    finally:
        await conn2.close()


@pytest.mark.asyncio
async def test_acquire_exits_on_contention() -> None:
    """If another connection already holds the lock, the helper logs and
    exits via SystemExit. Caller workers run under systemd Restart=always,
    so exiting is the right move."""
    name = "test_singleton_contended"
    url = _test_psycopg_url()

    holder = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        async with holder.cursor() as cur:
            await cur.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))", (name,),
            )
            row = await cur.fetchone()
        assert row is not None and row[0] is True, "test setup: holder failed"

        with pytest.raises(SystemExit):
            await acquire_singleton_lock(name, database_url=url)
    finally:
        await holder.close()
