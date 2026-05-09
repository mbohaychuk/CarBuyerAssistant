import pytest

from carbuyer.apps._runner import run_worker


def test_run_worker_runs_to_completion() -> None:
    fired = {"n": 0}

    async def main() -> None:
        fired["n"] += 1

    run_worker("test", main)
    assert fired["n"] == 1


def test_run_worker_propagates_exception() -> None:
    async def main() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_worker("test", main)
