from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import LotStatus
from carbuyer.db.session import get_session_maker

# Statuses representing "the lot is biddable right now" — shared by every view
# that filters to live inventory (feed, lot detail, closing-soon, health).
# Centralized here so adding a new biddable status updates one place, not four.
OPEN_STATUSES: tuple[str, ...] = (
    LotStatus.OPEN.value,
    LotStatus.CLOSING_SOON.value,
    LotStatus.EXTENDED.value,
)


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


# Auth seam — returns a stub for MVP. Wired into /health (cheapest seam-test) so
# the dependency signature is exercised on every commit; replacing the body
# with real auth is a one-line change.
def current_user() -> CurrentUser:
    return CurrentUser(id="me", role="dev")
