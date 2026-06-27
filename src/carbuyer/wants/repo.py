"""CRUD for want-list entries (rows in the `searches` table).

Plain async functions over a caller-owned session (the convention used by
db.upserts / dashboard query modules), not a repository class. Mutators flush to
populate PKs but leave the commit to the caller so a router/bot/worker controls
the transaction boundary. Criteria are stored as the JSONB `config`; callers read
them back with WantCriteria.model_validate(search.config).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Search
from carbuyer.wants.criteria import WantCriteria


async def create_want(
    session: AsyncSession,
    *,
    name: str,
    criteria: WantCriteria,
    user_id: str = "me",
) -> Search:
    want = Search(
        user_id=user_id,
        name=name,
        config=criteria.model_dump(mode="json"),
    )
    session.add(want)
    await session.flush()
    return want


async def get_want(session: AsyncSession, want_id: int) -> Search | None:
    return await session.get(Search, want_id)


async def list_wants(
    session: AsyncSession,
    *,
    user_id: str = "me",
    enabled_only: bool = False,
) -> list[Search]:
    stmt = select(Search).where(Search.user_id == user_id)
    if enabled_only:
        stmt = stmt.where(Search.enabled.is_(True))
    stmt = stmt.order_by(Search.id)
    return list((await session.execute(stmt)).scalars())


async def update_want(
    session: AsyncSession,
    want_id: int,
    *,
    name: str | None = None,
    criteria: WantCriteria | None = None,
    enabled: bool | None = None,
) -> Search | None:
    want = await session.get(Search, want_id)
    if want is None:
        return None
    if name is not None:
        want.name = name
    if criteria is not None:
        want.config = criteria.model_dump(mode="json")
    if enabled is not None:
        want.enabled = enabled
    await session.flush()
    return want


async def delete_want(session: AsyncSession, want_id: int) -> bool:
    want = await session.get(Search, want_id)
    if want is None:
        return False
    await session.delete(want)
    await session.flush()
    return True
