import pytest
from sqlalchemy import text

from carbuyer.db.session import get_session, make_engine


@pytest.mark.asyncio
async def test_make_engine_can_select_one() -> None:
    # Throwaway engine for direct connectivity check; conftest's session-scoped
    # engine fixture (Task 8) replaces the singleton without conflict.
    eng = make_engine()
    try:
        async with eng.connect() as conn:
            result = await conn.execute(text("SELECT 1 AS x"))
            assert result.scalar_one() == 1
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_get_session_yields_async_session() -> None:
    # Validates the singleton accessor path; does NOT dispose, since other
    # tests (post-Task-8) share the same engine via the conftest fixture.
    expected = 2
    async with get_session() as session:
        result = await session.execute(text("SELECT 2 AS x"))
        assert result.scalar_one() == expected
