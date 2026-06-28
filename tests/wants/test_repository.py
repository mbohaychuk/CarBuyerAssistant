from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Search
from carbuyer.wants import repo
from carbuyer.wants.criteria import WantCriteria


async def _reload(session: AsyncSession, want_id: int) -> Search | None:
    session.expire_all()
    return (
        await session.execute(select(Search).where(Search.id == want_id))
    ).scalar_one_or_none()


async def test_create_want_persists_name_and_criteria(session: AsyncSession) -> None:
    crit = WantCriteria(makes=["Nissan"], models=["Xterra"], transmissions=["manual"])
    want = await repo.create_want(session, name="manual xterra", criteria=crit)
    await session.commit()

    fetched = await _reload(session, want.id)
    assert fetched is not None
    assert fetched.name == "manual xterra"
    assert fetched.user_id == "me"
    assert fetched.enabled is True
    assert WantCriteria.model_validate(fetched.config) == crit


async def test_get_want_returns_none_for_missing(session: AsyncSession) -> None:
    assert await repo.get_want(session, 999_999) is None


async def test_create_want_rejects_empty_name(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="name"):
        await repo.create_want(session, name="   ", criteria=WantCriteria())


async def test_create_want_rejects_overlong_name(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="name"):
        await repo.create_want(session, name="x" * 200, criteria=WantCriteria())


async def test_list_wants_all_then_enabled_only(session: AsyncSession) -> None:
    live = await repo.create_want(session, name="live", criteria=WantCriteria())
    muted = await repo.create_want(session, name="muted", criteria=WantCriteria())
    await repo.update_want(session, muted.id, enabled=False)
    await session.commit()

    all_ids = {w.id for w in await repo.list_wants(session)}
    assert {live.id, muted.id} <= all_ids

    enabled_ids = {w.id for w in await repo.list_wants(session, enabled_only=True)}
    assert live.id in enabled_ids
    assert muted.id not in enabled_ids


async def test_update_want_changes_name_criteria_enabled(session: AsyncSession) -> None:
    want = await repo.create_want(session, name="old", criteria=WantCriteria())
    new_crit = WantCriteria(makes=["Lexus"], models=["GX 470"], price_ceiling_cad=20000)
    await repo.update_want(session, want.id, name="new", criteria=new_crit, enabled=False)
    await session.commit()

    fetched = await _reload(session, want.id)
    assert fetched is not None
    assert fetched.name == "new"
    assert fetched.enabled is False
    assert WantCriteria.model_validate(fetched.config) == new_crit


async def test_update_want_returns_none_for_missing(session: AsyncSession) -> None:
    assert await repo.update_want(session, 999_999, name="x") is None


async def test_list_wants_is_scoped_to_user(session: AsyncSession) -> None:
    mine = await repo.create_want(session, name="mine", criteria=WantCriteria())
    await repo.create_want(
        session, name="theirs", criteria=WantCriteria(), user_id="someone_else"
    )
    await session.commit()

    wants = await repo.list_wants(session)  # defaults to user_id="me"
    assert mine.id in {w.id for w in wants}
    assert all(w.user_id == "me" for w in wants)


async def test_update_want_leaves_unspecified_fields_intact(session: AsyncSession) -> None:
    crit = WantCriteria(makes=["Nissan"], models=["Xterra"])
    want = await repo.create_want(session, name="orig", criteria=crit)
    await session.commit()

    await repo.update_want(session, want.id, name="renamed")  # only the name
    await session.commit()

    fetched = await _reload(session, want.id)
    assert fetched is not None
    assert fetched.name == "renamed"
    assert fetched.enabled is True  # untouched
    assert WantCriteria.model_validate(fetched.config) == crit  # untouched


async def test_delete_want_removes_row(session: AsyncSession) -> None:
    want = await repo.create_want(session, name="doomed", criteria=WantCriteria())
    await session.commit()

    assert await repo.delete_want(session, want.id) is True
    await session.commit()
    assert await _reload(session, want.id) is None
    assert await repo.delete_want(session, want.id) is False
