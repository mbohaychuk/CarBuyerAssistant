"""Phase 11 Task 48 — action endpoints (mark / notes / admin rescore)."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.apps.dashboard.deps import CurrentUser, current_user, require_admin
from carbuyer.db.enums import UserAction, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot, LotActionHistory


def _seed_lot(
    session: AsyncSession,
    *,
    user_action: str | None = None,
    max_bid_cad: Decimal | None = None,
    bid_placed_at: datetime | None = None,
) -> AuctionLot:
    a = Auction(
        source="hibid", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    lot = AuctionLot(
        auction=a, source_lot_id="L1", url="https://x/lot/L1",
        title="Test", current_high_bid_cad=Decimal("1000"),
    )
    if user_action is not None:
        lot.user_action = UserAction(user_action)
    if max_bid_cad is not None:
        lot.max_bid_cad = max_bid_cad
    if bid_placed_at is not None:
        lot.bid_placed_at = bid_placed_at
    session.add(lot)
    return lot


@pytest.fixture
def _patch_deps(  # pyright: ignore[reportUnusedFunction]
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


# ─── /lots/{id}/mark ───


@pytest.mark.asyncio
async def test_mark_endpoint_updates_user_action(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark", data={"action": "interested"},
        )
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(AuctionLot, lot_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.user_action == UserAction.INTERESTED.value


@pytest.mark.asyncio
async def test_mark_endpoint_404_when_lot_missing(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/lots/999999/mark", data={"action": "interested"},
        )
    assert r.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_mark_toggle_off_clears_user_action(_patch_deps: AsyncSession) -> None:
    """Clicking the already-active button clears the state to NULL.
    The macro sends `currently_active=true` from the button's own
    data-active attribute; the server treats that as a toggle-off."""
    session = _patch_deps
    lot = _seed_lot(session)
    lot.user_action = UserAction.INTERESTED.value
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark",
            data={"action": "interested", "currently_active": "true"},
        )
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(AuctionLot, lot_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.user_action is None


@pytest.mark.asyncio
async def test_mark_passed_then_toggle_off(_patch_deps: AsyncSession) -> None:
    """Same toggle semantics apply to Pass: click once to set
    PASSED, click again with currently_active=true to clear."""
    session = _patch_deps
    lot = _seed_lot(session)
    lot.user_action = UserAction.PASSED.value
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark",
            data={"action": "passed", "currently_active": "true"},
        )
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(AuctionLot, lot_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.user_action is None


@pytest.mark.asyncio
async def test_mark_htmx_toggle_off_renders_no_active_button(
    _patch_deps: AsyncSession,
) -> None:
    """The HTMX fragment returned after a toggle-off must not flag any
    button as active — otherwise the user clicks and the UI still shows
    the state they tried to clear."""
    session = _patch_deps
    lot = _seed_lot(session)
    lot.user_action = UserAction.INTERESTED.value
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark",
            data={"action": "interested", "currently_active": "true"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200  # noqa: PLR2004
    # No button should have data-active="true" in the rendered fragment.
    assert 'data-active="true"' not in r.text


@pytest.mark.asyncio
async def test_mark_endpoint_rejects_invalid_action(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark", data={"action": "garbage"},
        )
    assert r.status_code == 422  # noqa: PLR2004


# ─── /lots/{id}/notes ───


@pytest.mark.asyncio
async def test_notes_appends_to_existing(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.notes = "first"
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/notes", data={"note": "second"},
        )
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(AuctionLot, lot_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.notes == "first\nsecond"


@pytest.mark.asyncio
async def test_notes_writes_when_empty(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/notes", data={"note": "hello"},
        )
    assert r.status_code == 204  # noqa: PLR2004

    fresh = await session.get(AuctionLot, lot_id)
    assert fresh is not None
    await session.refresh(fresh)
    assert fresh.notes == "hello"


@pytest.mark.asyncio
async def test_notes_404_when_lot_missing(_patch_deps: AsyncSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/lots/999999/notes", data={"note": "x"},
        )
    assert r.status_code == 404  # noqa: PLR2004


# ─── /admin/rescore ───


@pytest.mark.asyncio
async def test_rescore_resets_valuation_status(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    lot.valuation_status = ValuationStatus.DONE.value
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/admin/rescore")
    assert r.status_code == 204  # noqa: PLR2004

    statuses = list((await session.execute(
        select(AuctionLot.valuation_status).where(AuctionLot.id == lot_id),
    )).scalars().all())
    assert statuses == [ValuationStatus.PENDING.value]


# ─── auth seam ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_requires_authenticated_user(_patch_deps: AsyncSession) -> None:
    """Phase 13: every mutating endpoint must call current_user. Override the
    dependency to raise 401 and confirm the endpoint propagates it."""
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    def _denied(_request: Request) -> CurrentUser:
        raise HTTPException(status_code=401, detail="auth required")

    app.dependency_overrides[current_user] = _denied
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                f"/lots/{lot_id}/mark", data={"action": "interested"},
            )
        assert r.status_code == 401  # noqa: PLR2004
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_notes_requires_authenticated_user(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()
    lot_id = lot.id

    def _denied(_request: Request) -> CurrentUser:
        raise HTTPException(status_code=401, detail="auth required")

    app.dependency_overrides[current_user] = _denied
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                f"/lots/{lot_id}/notes", data={"note": "x"},
            )
        assert r.status_code == 401  # noqa: PLR2004
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rescore_requires_admin(_patch_deps: AsyncSession) -> None:
    """admin-only endpoint must reject a non-admin via require_admin's check."""
    def _denied(
        _user: CurrentUser = CurrentUser(id="x", role="dev"),  # noqa: B008
    ) -> CurrentUser:
        raise HTTPException(status_code=403, detail="admin required")

    app.dependency_overrides[require_admin] = _denied
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/admin/rescore")
        assert r.status_code == 403  # noqa: PLR2004
    finally:
        app.dependency_overrides.clear()


# ─── Phase 4: apply_user_action integration ────────────────────────────────


@pytest.mark.asyncio
async def test_mark_bid_placed_writes_history_and_amount(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session, user_action=UserAction.INTERESTED.value)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark",
            data={"action": "bid_placed", "max_bid_cad": "500"},
        )
    assert r.status_code in (200, 204)

    await session.refresh(lot)
    assert lot.user_action == UserAction.BID_PLACED
    assert lot.max_bid_cad == Decimal("500")
    assert lot.bid_placed_at is not None

    history = list((await session.execute(
        select(LotActionHistory).where(LotActionHistory.lot_id == lot_id),
    )).scalars().all())
    assert len(history) == 1
    assert history[0].source == "dashboard"


@pytest.mark.asyncio
async def test_mark_bid_placed_without_amount_returns_422(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session, user_action=UserAction.INTERESTED.value)
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark",
            data={"action": "bid_placed"},
        )
    assert r.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_toggle_off_clears_all_bound_fields(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(
        session,
        user_action=UserAction.BID_PLACED.value,
        max_bid_cad=Decimal("500"),
        bid_placed_at=datetime.now(UTC),
    )
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/lots/{lot_id}/mark",
            data={"action": "bid_placed", "currently_active": "true"},
        )
    assert r.status_code in (200, 204)

    await session.refresh(lot)
    assert lot.user_action is None
    assert lot.max_bid_cad is None
    assert lot.bid_placed_at is None


@pytest.mark.asyncio
async def test_mark_from_watchlist_returns_board_fragment(
    _patch_deps: AsyncSession,
) -> None:
    """A click on a card sitting in the Interested column transitions
    it to bid_placed; the response is the whole board partial so the
    card reappears in the Bid placed column."""
    session = _patch_deps
    lot = _seed_lot(session, user_action="interested")
    await session.commit()
    lot_id = lot.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            f"/lots/{lot_id}/mark",
            data={"action": "passed"},
            headers={"HX-Request": "true", "HX-Target": "watchlist-board"},
        )
    assert r.status_code == 200  # noqa: PLR2004
    assert 'id="watchlist-board"' in r.text
    # The lot is now in the Passed column, not Interested.
    assert r.text.count("Passed") >= 1


@pytest.mark.asyncio
async def test_bid_modal_renders_form(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(session)
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(
            f"/lots/{lot.id}/bid-modal?return_target=lot-{lot.id}",
        )
    assert r.status_code == 200  # noqa: PLR2004
    assert 'name="max_bid_cad"' in r.text
    assert 'name="action"' in r.text and "bid_placed" in r.text
    assert f'hx-target="#lot-{lot.id}"' in r.text


@pytest.mark.asyncio
async def test_bid_modal_prefills_in_raise_max_mode(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    lot = _seed_lot(
        session, user_action="bid_placed", max_bid_cad=Decimal("4200"),
        bid_placed_at=datetime.now(UTC),
    )
    await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(
            f"/lots/{lot.id}/bid-modal?return_target=lot-{lot.id}",
        )
    assert r.status_code == 200  # noqa: PLR2004
    assert 'value="4200"' in r.text


@pytest.mark.asyncio
async def test_modal_dismiss_returns_empty(
    _patch_deps: AsyncSession,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/modal/dismiss")
    assert r.status_code == 200  # noqa: PLR2004
    assert r.text == ""


@pytest.mark.asyncio
async def test_rescore_emits_valuation_pending_notify(
    _patch_deps: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, the valuator (LISTEN-only) sees the rescore only at its
    next restart's catchup sweep."""
    from carbuyer.apps.dashboard.routers import actions as actions_mod

    calls: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str) -> None:
        calls.append((channel, payload))

    monkeypatch.setattr(actions_mod, "notify", fake_notify)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/admin/rescore")
    assert r.status_code == 204  # noqa: PLR2004
    assert calls == [("valuation_pending", "")]
