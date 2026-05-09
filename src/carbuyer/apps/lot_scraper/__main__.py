from carbuyer.apps._runner import run_worker
from carbuyer.apps.lot_scraper.scraper import main

if __name__ == "__main__":
    run_worker("lot_scraper", main)
