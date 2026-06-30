from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.digest import digest as digest_mod
from carbuyer.db.models import PrivateListing, Search, WantMatch

_DIGEST_NOW = datetime(2026, 6, 30, 8, 0, tzinfo=UTC)


async def _seed_unnotified_match(
    session: AsyncSession, *, key: str = "K1", enabled: bool = True, score: float = 0.01,
) -> int:
    listing = PrivateListing(
        source="kijiji", source_listing_id=key, url=f"http://k/{key}",
        title="2010 Nissan Xterra", make="Nissan", model="Xterra", year=2010,
        asking_price_cad=Decimal("9900"), seller_type="private",
        location_province="AB", listing_status="active",
        expected_value_cad=Decimal("10000"), value_mid_cad=Decimal("10000"), comp_count=9,
    )
    want = Search(name="xterra", config={}, enabled=enabled)
    session.add_all([listing, want])
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=listing.id, want_relative_score=score)
    session.add(wm)
    await session.flush()
    return wm.id


@pytest.fixture
def _patched_get_session(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    """Patch the digest module's get_session to share the test transaction."""
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(digest_mod, "get_session", fake_get_session)
    return session


def _patch_discord(monkeypatch: pytest.MonkeyPatch, *, post_ok: bool, posted: list[object]) -> None:
    async def fake_resolve(channels: object, **_: object) -> dict[str, int]:
        return {"wants": 4242}

    async def fake_post(channel_id: int, content: str, *, session: object = None) -> bool:
        posted.append((channel_id, content))
        return post_ok

    monkeypatch.setattr(digest_mod, "resolve_channels", fake_resolve)
    monkeypatch.setattr(digest_mod, "post_simple_message", fake_post)
    monkeypatch.setattr("carbuyer.apps.digest.digest.settings.discord_bot_token", "tok")


# ─── build_digest ───


@pytest.mark.asyncio
async def test_build_digest_groups_unnotified_enabled_matches(session: AsyncSession) -> None:
    await _seed_unnotified_match(session, key="K1")
    await _seed_unnotified_match(session, key="K2", enabled=False)  # muted want excluded
    await session.flush()

    match_ids, groups = await digest_mod.build_digest(session)
    assert len(match_ids) == 1
    assert groups and groups[0][0] == "xterra"
    assert groups[0][1][0].title.endswith("Nissan Xterra")


@pytest.mark.asyncio
async def test_build_digest_empty_when_nothing_unnotified(session: AsyncSession) -> None:
    match_ids, groups = await digest_mod.build_digest(session)
    assert match_ids == []
    assert groups == []


# ─── main ───


@pytest.mark.asyncio
async def test_main_posts_one_digest_and_stamps(
    _patched_get_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    wm_id = await _seed_unnotified_match(session)
    await session.flush()
    posted: list[object] = []
    _patch_discord(monkeypatch, post_ok=True, posted=posted)

    await digest_mod.main(now=_DIGEST_NOW)

    assert len(posted) == 1
    session.expire_all()
    refreshed = await session.get(WantMatch, wm_id)
    assert refreshed is not None
    assert refreshed.notified_at is not None  # delivered, stamped once


@pytest.mark.asyncio
async def test_main_post_failure_leaves_matches_unnotified(
    _patched_get_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched_get_session
    wm_id = await _seed_unnotified_match(session)
    await session.flush()
    posted: list[object] = []
    _patch_discord(monkeypatch, post_ok=False, posted=posted)

    await digest_mod.main(now=_DIGEST_NOW)

    session.expire_all()
    refreshed = await session.get(WantMatch, wm_id)
    assert refreshed is not None
    assert refreshed.notified_at is None  # un-notified, retried next run


@pytest.mark.asyncio
async def test_main_does_not_post_when_empty(
    _patched_get_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[object] = []
    _patch_discord(monkeypatch, post_ok=True, posted=posted)

    await digest_mod.main(now=_DIGEST_NOW)

    assert posted == []  # nothing un-notified → no post
