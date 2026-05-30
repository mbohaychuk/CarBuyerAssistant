from carbuyer.apps._runner import run_worker
from carbuyer.apps.auction_digest.runner import main

if __name__ == "__main__":
    run_worker("auction_digest", main)
