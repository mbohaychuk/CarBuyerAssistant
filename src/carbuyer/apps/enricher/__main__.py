from carbuyer.apps._runner import run_worker
from carbuyer.apps.enricher.enricher import main

if __name__ == "__main__":
    run_worker("enricher", main)
