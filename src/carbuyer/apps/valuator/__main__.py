from carbuyer.apps._runner import run_worker
from carbuyer.apps.valuator.valuator import main

if __name__ == "__main__":
    run_worker("valuator", main)
