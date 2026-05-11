from carbuyer.apps._runner import run_worker
from carbuyer.apps.vision_batcher.batcher import main

if __name__ == "__main__":
    run_worker("vision_batcher", main)
