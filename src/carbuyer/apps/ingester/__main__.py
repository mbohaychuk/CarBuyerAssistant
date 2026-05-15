from __future__ import annotations

from carbuyer.apps._runner import run_worker
from carbuyer.apps.ingester.ingester import main

if __name__ == "__main__":
    run_worker("ingester", main)
