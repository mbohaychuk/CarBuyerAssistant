from carbuyer.apps._runner import run_worker
from carbuyer.apps.private_sale.worker import main

if __name__ == "__main__":
    run_worker("private_sale", main)
