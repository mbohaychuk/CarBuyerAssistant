from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_app() -> FastAPI:
    app = FastAPI(title="CarBuyer Dashboard")
    app.mount(
        "/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="static",
    )
    # Inner import: routers import `templates` from this module, so hoisting
    # them creates a circular import. Tied to create_app's lifecycle anyway.
    from carbuyer.apps.dashboard.routers import (  # noqa: PLC0415
        actions,
        closing,
        comps,
        feed,
        health,
        lots,
        purchases,
        sold,
        watched,
    )
    for router in (
        feed, closing, watched, lots, comps, sold, purchases, health, actions,
    ):
        app.include_router(router.router)
    return app


app = create_app()
