from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from carbuyer.shared.logging import configure_logging, get_logger


def run_worker(name: str, main: Callable[[], Awaitable[None]]) -> None:
    """Run an async worker with graceful shutdown on SIGTERM/SIGINT.

    On signal:
      1. The worker task is cancelled.
      2. CancelledError propagates out of ``main()``.
      3. ``loop.shutdown_asyncgens()`` runs to ensure async-generator finalizers
         (e.g. the psycopg LISTEN connection in ``db.notify.listen()``) are
         awaited.
      4. The loop closes.
    """
    configure_logging()
    log = get_logger(name)
    loop = asyncio.new_event_loop()

    async def runner() -> None:
        log.info("worker starting")
        try:
            await main()
        except asyncio.CancelledError:
            log.info("worker cancelled")
            raise
        except Exception:
            log.exception("worker crashed")
            raise
        finally:
            log.info("worker exiting")

    task = loop.create_task(runner())

    def stop(*_: object) -> None:
        if not task.done():
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
