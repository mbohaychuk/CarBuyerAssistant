from carbuyer.apps._runner import run_worker
from carbuyer.apps.notifier.notifier import main

if __name__ == "__main__":
    run_worker("notifier", main)
