from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
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


# Auth seam — returns a stub for MVP, but wired into every mutating endpoint
# (mark / notes / rescore / retry_routing / purchases_create) so the dependency
# signature is exercised on every request that changes state. Replacing the
# body with real auth (e.g. session cookie + DB lookup) is a one-file change.
#
# `request: Request` is accepted now (unused) so the future real implementation
# can read headers/cookies without changing every caller's signature.
def current_user(request: Request) -> CurrentUser:  # noqa: ARG001
    return CurrentUser(id="me", role="dev")


# Admin-only seam. Today every authenticated user passes; future real auth can
# check role/scope. Keeps the /admin/* endpoints dependency-gated separately
# from regular mutating endpoints so adding a real role check is one place.
def require_admin(
    user: Annotated[CurrentUser, Depends(current_user)],
) -> CurrentUser:
    # Future: if user.role != "admin": raise HTTPException(403, ...). The stub
    # currently returns role="dev" which passes the seam check.
    if user.role not in {"admin", "dev"}:
        raise HTTPException(status_code=403, detail="admin required")
    return user
