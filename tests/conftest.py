from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from carbuyer.db.base import Base
from carbuyer.db.session import set_engine_for_testing
from carbuyer.shared.config import settings


def _test_url() -> str:
    # Append _test to the DB name. Test DB is auto-created by the
    # /docker-entrypoint-initdb.d/ script in infra/docker-compose.yml.
    url = settings.database_url
    if url.endswith("/carbuyer"):
        return url[: -len("/carbuyer")] + "/carbuyer_test"
    if url.endswith("/carbuyer_test"):
        return url
    raise RuntimeError(
        f"Refusing to run tests against non-carbuyer database URL: {url}"
    )


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    # NullPool: each connection is fresh — no pool deadlocks, no cross-test
    # connection state. Schema is built once per session; tests use savepoints
    # for isolation (see `session` fixture).
    eng = create_async_engine(_test_url(), poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    # Make get_session() / get_session_maker() in production code resolve to
    # this test engine for every test in the session.
    await set_engine_for_testing(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Per-test AsyncSession that rolls back on teardown.

    Pattern: open one outer connection, begin an outer transaction, bind a
    sessionmaker to that connection with `join_transaction_mode='create_savepoint'`
    so every commit inside a test becomes a SAVEPOINT release; the outer
    rollback at teardown undoes everything. No drop/create per test.

    The bound maker is exposed via `session.info["maker"]` so tests that need
    to simulate fresh `get_session()` calls (e.g. enricher tests) can create
    additional sessions sharing the same outer transaction.
    """
    async with engine.connect() as conn:
        outer = await conn.begin()
        maker = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as s:
            s.info["maker"] = maker
            yield s
        await outer.rollback()
