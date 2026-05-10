"""Bid-poller worker entry point."""

from __future__ import annotations

from carbuyer.apps._runner import run_worker
from carbuyer.apps.bid_poller.poller import main

if __name__ == "__main__":
    run_worker("bid_poller", main)
