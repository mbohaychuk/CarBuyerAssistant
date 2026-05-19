from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# All scheduled_end_at values in the DB are UTC. The dashboard renders for a
# single operator (Alberta), so localizing at the template layer is simpler
# than threading tz through every router. Rendering UTC verbatim surprises
# users who read "21:00" as 9pm local; this filter applies the conversion
# in one place. Swap to per-request lookup if the dashboard ever multi-tenants.
_DISPLAY_TZ = ZoneInfo("America/Edmonton")


def _local_dt(dt: datetime | None, fmt: str = "%b %d %H:%M") -> str:
    if dt is None:
        return "?"
    return dt.astimezone(_DISPLAY_TZ).strftime(fmt)


templates.env.filters["local_dt"] = _local_dt

# now_utc() is exposed as a global Jinja callable so macros that need a
# "right now" anchor (e.g. the countdown chip) can compute relative time
# without each router having to thread one through their context. Routers
# may still pass an explicit `now` kwarg for deterministic tests.
templates.env.globals["now_utc"] = lambda: datetime.now(UTC)


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
        admin,
        auctions,
        closing,
        comps,
        feed,
        health,
        lots,
        needs_plugin,
        purchases,
        sold,
        watched,
    )
    for router in (
        feed, closing, watched, lots, comps, sold, purchases, health, actions,
        needs_plugin, auctions, admin,
    ):
        app.include_router(router.router)
    return app


app = create_app()
