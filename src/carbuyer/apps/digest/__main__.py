from carbuyer.apps._runner import run_worker
from carbuyer.apps.digest.digest import main

if __name__ == "__main__":
    run_worker("digest", main)
