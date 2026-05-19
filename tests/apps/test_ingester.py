"""Ingester dispatch isolation tests.

The load-bearing property of `_dispatch_strategies` is that one strategy
raising must NOT abort sibling strategies in the same run. This is the only
test surface for that guarantee — the strategies themselves are tested
elsewhere (carbuyer.sources.* fixtures).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from carbuyer.apps.ingester import ingester


@pytest.fixture
def _restore_strategies() -> None:
    """Save and restore the global STRATEGIES list around each test so
    tests can mutate it without bleeding into siblings."""
    saved = list(ingester.STRATEGIES)
    yield
    ingester.STRATEGIES.clear()
    ingester.STRATEGIES.extend(saved)


def _make_strategy(*, returns: int | None = 0, raises: type[BaseException] | None = None,
                   ) -> Callable[[], Awaitable[int]]:
    async def _strategy() -> int:
        if raises is not None:
            raise raises("boom")
        assert returns is not None  # narrowing
        return returns

    return _strategy


@pytest.mark.asyncio
async def test_dispatch_one_strategy_failing_does_not_abort_siblings(
    _restore_strategies: None,
) -> None:
    # Three registered strategies: A succeeds, B raises, C must still run.
    # Without per-strategy try/except, C would be skipped.
    ran: list[str] = []

    async def a() -> int:
        ran.append("a")
        return 5

    async def b() -> int:
        ran.append("b")
        raise RuntimeError("b explodes")

    async def c() -> int:
        ran.append("c")
        return 7

    ingester.STRATEGIES.clear()
    ingester.STRATEGIES.extend([("a", a), ("b", b), ("c", c)])

    results = await ingester._dispatch_strategies()

    # All three ran (no abort on B's exception).
    assert ran == ["a", "b", "c"]
    # Per-strategy counts reflected; B reports None for the raise.
    assert results == {"a": 5, "b": None, "c": 7}


@pytest.mark.asyncio
async def test_dispatch_empty_strategy_list_is_a_no_op(
    _restore_strategies: None,
) -> None:
    # Defensive: an empty STRATEGIES list (e.g. all sources gated off) must
    # complete cleanly with an empty result map, not raise or hang.
    ingester.STRATEGIES.clear()
    results = await ingester._dispatch_strategies()
    assert results == {}


@pytest.mark.asyncio
async def test_dispatch_binds_strategy_contextvar_for_each_invocation(
    _restore_strategies: None,
) -> None:
    """_dispatch_strategies wraps each strategy call in
    `bound_contextvars(strategy=name)` so all log lines inside (including
    any source-plugin log lines emitted during the strategy run) get
    strategy attribution merged in by the configured processor chain.

    Verified by having each strategy capture its own contextvars at call
    time — the binding must be the *current* iteration's name, not stale
    from the previous one.
    """
    import structlog  # noqa: PLC0415

    captured: dict[str, dict[str, object]] = {}

    def _capture(name: str) -> Callable[[], Awaitable[int]]:
        async def _strategy() -> int:
            captured[name] = dict(structlog.contextvars.get_contextvars())
            return 1

        return _strategy

    ingester.STRATEGIES.clear()
    ingester.STRATEGIES.extend([
        ("first", _capture("first")),
        ("second", _capture("second")),
    ])
    structlog.contextvars.clear_contextvars()

    await ingester._dispatch_strategies()

    assert captured["first"].get("strategy") == "first"
    assert captured["second"].get("strategy") == "second"
    # Binding must be unbound on dispatch exit — otherwise the next ingester
    # run (or the calling main()) inherits stale attribution.
    assert "strategy" not in structlog.contextvars.get_contextvars()
