from carbuyer.apps._runner import run_worker
from carbuyer.apps.search_matcher.worker import main

if __name__ == "__main__":
    run_worker("search_matcher", main)
