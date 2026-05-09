from carbuyer.apps._runner import run_worker
from carbuyer.apps.auction_discoverer.discoverer import main

if __name__ == "__main__":
    run_worker("auction_discoverer", main)
