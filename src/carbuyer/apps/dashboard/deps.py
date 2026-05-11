from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.session import get_session_maker


@dataclass(slots=True, frozen=True)
class CurrentUser:
    id: str
    role: str


# Plain async generator (not @asynccontextmanager): FastAPI dependencies need a
# yielding callable, not a context-manager factory. The maker lookup happens per
# request so test fixtures can monkeypatch `get_session_maker` on this module.
async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_maker()() as session:
        yield session


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def current_user() -> CurrentUser:
    return CurrentUser(id="me", role="dev")
