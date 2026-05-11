from carbuyer.apps._runner import run_worker
from carbuyer.apps.auction_distiller.distiller import main

if __name__ == "__main__":
    run_worker("auction_distiller", main)
