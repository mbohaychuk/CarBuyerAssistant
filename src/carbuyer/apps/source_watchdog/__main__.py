from __future__ import annotations

from carbuyer.apps._runner import run_worker
from carbuyer.apps.source_watchdog.watchdog import main

if __name__ == "__main__":
    run_worker("source_watchdog", main)
