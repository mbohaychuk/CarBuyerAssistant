from carbuyer.apps._runner import run_worker
from carbuyer.apps.bot.bot import main

if __name__ == "__main__":
    run_worker("bot", main)
