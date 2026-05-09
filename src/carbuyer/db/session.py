from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from carbuyer.shared.config import settings


# Pool sizing rationale: 11 worker processes; each opens its own pool. With
# pool_size=2, max_overflow=3 the absolute upper bound is 5 * 11 = 55 sessions.
# Postgres is configured for max_connections=200 (see infra/docker-compose.yml),
# leaving headroom for ops, dashboard, and Alembic. Workers that do bursty DB
# I/O can override per-process via make_engine(pool_size=..., max_overflow=...).
def make_engine(
    url: str | None = None,
    *,
    pool_size: int = 2,
    max_overflow: int = 3,
) -> AsyncEngine:
    return create_async_engine(
        url or settings.database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            # statement_timeout=30s catches stuck OLTP queries; long-running
            # comp aggregates in the valuator/distiller must SET LOCAL their own
            # higher limit inside their transaction.
            "options": (
                "-c statement_timeout=30000 "
                "-c idle_in_transaction_session_timeout=60000 "
                "-c lock_timeout=5000"
            ),
        },
    )


_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine (created lazily on first call)."""
    global _engine  # noqa: PLW0603  -- intentional process-wide singleton
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide AsyncSession factory bound to get_engine()."""
    global _session_maker  # noqa: PLW0603  -- intentional process-wide singleton
    if _session_maker is None:
        _session_maker = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_maker


async def set_engine_for_testing(engine: AsyncEngine) -> None:
    """Test-only: replace the cached engine and session factory with a test pool.

    Disposes any previously cached engine. After this call, every callsite that
    uses get_session() / get_session_maker() / get_engine() will see the test
    engine, including modules that imported the names earlier (since they are
    looked up via these accessors, not module-level globals).
    """
    global _engine, _session_maker  # noqa: PLW0603  -- test-only override
    if _engine is not None and _engine is not engine:
        await _engine.dispose()
    _engine = engine
    _session_maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_maker()() as session:
        yield session
