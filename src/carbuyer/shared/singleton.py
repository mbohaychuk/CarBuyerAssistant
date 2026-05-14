"""Process-wide singleton enforcement via Postgres advisory locks.

Each continuous worker (notifier, enricher, valuator, bid_poller, lot_scraper)
must run as a single instance. They share workload-queue tables and the
SELECT FOR UPDATE SKIP LOCKED + recover_orphans pattern assumes no concurrent
claimer exists at startup. An operator running `python -m carbuyer.apps.X`
from a shell while the systemd unit is also running breaks that assumption
and can produce duplicate Discord posts or double-processed enrichments.

Mechanism:
  - Each worker's main() calls acquire_singleton_lock("<worker_name>") FIRST.
  - The helper opens a dedicated psycopg connection (NOT a pooled session)
    and calls SELECT pg_try_advisory_lock(hashtext(name)).
  - The returned connection MUST be held for the process lifetime — closing
    it releases the lock.
  - On contention the helper exits with non-zero status; systemd's
    Restart=always cycles the worker, and if a real peer is still alive the
    next retry will also fail, which is the desired symptom.

The lock keyspace is per-database (pg_advisory_lock is database-scoped), so
distinct worker names within one DB don't collide and tests against
carbuyer_test never interfere with production locks on carbuyer.
"""
from __future__ import annotations

import sys

import psycopg

from carbuyer.db.notify import to_psycopg_url
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("singleton")


async def acquire_singleton_lock(
    name: str,
    *,
    database_url: str | None = None,
) -> psycopg.AsyncConnection:
    """Acquire a process-wide singleton lock for the named worker.

    Returns the dedicated connection that holds the lock. The caller MUST
    keep it open for the lifetime of the process — closing the connection
    releases the lock.

    On contention (another instance already holds the lock), logs an error
    and exits via SystemExit. Callers are workers under systemd supervision,
    so exiting is the right move: the supervisor's Restart=always cycles the
    process and the contended state will re-evaluate on the next attempt.
    """
    psycopg_url = to_psycopg_url(database_url or settings.database_url)
    conn = await psycopg.AsyncConnection.connect(psycopg_url, autocommit=True)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            (name,),
        )
        row = await cur.fetchone()
    got = bool(row[0]) if row else False
    if not got:
        await conn.close()
        log.error(
            "singleton lock unavailable; another instance is running",
            worker=name,
        )
        sys.exit(f"singleton lock unavailable for {name}; exiting")
    log.info("singleton lock acquired", worker=name)
    return conn
