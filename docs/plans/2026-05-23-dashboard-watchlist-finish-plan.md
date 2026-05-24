# Dashboard Watchlist Finish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Fresh subagent per task + two-stage review (spec compliance → code quality).

**Goal:** Finish the watchlist redesign — `/watched` becomes a 4-column kanban,
the "Bid placed" button captures `max_bid_cad` via a modal, and the lot-detail
page renders the `LotActionHistory` timeline.

**Architecture:** Zero new JS / zero new dependencies. The kanban is HTMX +
CSS grid + scroll-snap; cards move via the existing Watch / Bid / Pass buttons
and `/mark` re-renders the whole board when targeted at `#watchlist-board`.
The modal is a CSS-only `position: fixed` overlay swapped into a top-level
`#modal-slot`, dismissed by swapping empty content back in. The activity
timeline is a new read helper (`lot_action_history`) co-located with the
writer (`apply_user_action`) in `db/lot_state.py`.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2 async, Jinja2, HTMX,
Tailwind v4 (`make css`). Test runner: `uv run pytest -q`.

**Spec:** `docs/specs/2026-05-20-dashboard-watchlist-finish-design.md`

**Branch:** `dashboard-watchlist-finish` (already created off main, two
commits in: README accuracy fix + the spec). TDD throughout — failing test
first.

---

## File Structure

### PR 4 — Watchlist kanban
- **Modify** `src/carbuyer/apps/dashboard/routers/watched.py` — extract a
  public `build_watchlist_buckets()` helper; the route uses it.
- **Create** `src/carbuyer/apps/dashboard/templates/partials/watchlist_board.html`
  — the `#watchlist-board` wrapper + four columns. Rendered by both
  `/watched` (page) and `/mark` (board response).
- **Modify** `src/carbuyer/apps/dashboard/templates/pages/watched.html` —
  delegates to the partial.
- **Modify** `src/carbuyer/apps/dashboard/routers/actions.py` — add the
  `HX-Target == "watchlist-board"` response branch.
- **Modify** `src/carbuyer/apps/dashboard/templates/partials/lot_card.html`
  — thread an optional `action_target` to `action_buttons`.
- **Create** `src/carbuyer/apps/dashboard/static/css/components/watchlist.css`
  — grid + scroll-snap rules.
- **Modify** `src/carbuyer/apps/dashboard/static/css/tailwind.css` — import
  the new component CSS file.

### PR 5 — Place-bid modal
- **Modify** `src/carbuyer/apps/dashboard/templates/base.html` — add
  `<div id="modal-slot"></div>`.
- **Create** `src/carbuyer/apps/dashboard/templates/partials/bid_modal.html`
  — the overlay + form.
- **Modify** `src/carbuyer/apps/dashboard/routers/actions.py` — add
  `GET /lots/{id}/bid-modal` and `GET /modal/dismiss`; append the OOB
  modal-clear to successful `bid_placed` responses via a template flag.
- **Modify** `src/carbuyer/apps/dashboard/templates/_macros.html` — the
  `action_buttons` macro renders the bid button as a `hx-get` modal trigger
  carrying `return_target`.
- **Modify** `src/carbuyer/apps/dashboard/templates/partials/lot_card.html`
  and `src/carbuyer/apps/dashboard/templates/partials/watchlist_board.html`
  — render the OOB modal-clear at the end when `oob_clear_modal` is set.
- **Modify** `lot_card.html` and the lot-detail decision-card markup —
  display `max $X` on `bid_placed` lots.
- **Create CSS rules** for `.modal` in `static/css/components/watchlist.css`
  (or a dedicated `modal.css`; co-locate in `watchlist.css` to keep the new
  CSS files to one).

### PR 6 — Action history
- **Modify** `src/carbuyer/db/lot_state.py` — add async
  `lot_action_history(session, lot_id)` reader.
- **Modify** `src/carbuyer/apps/dashboard/routers/lots.py` — `lot_detail`
  fetches history, passes it in context.
- **Modify** `src/carbuyer/apps/dashboard/templates/pages/lot_detail.html`
  — new "Activity" `<section>` after Comparable sales.
- **Modify** `src/carbuyer/apps/dashboard/static/css/components/lot-detail.css`
  — timeline rules.

### Tests
- **Modify/extend** `tests/apps/dashboard/test_views.py` (or a new
  `test_watched.py` — see Task 1) for the kanban view tests.
- **Modify/extend** `tests/apps/dashboard/test_actions.py` for the new
  `/mark` branch, the `/bid-modal` route, the dismiss route, and the OOB clear.
- **Modify/extend** `tests/apps/dashboard/test_lot_detail.py` for the
  activity section.
- **Create** `tests/db/test_lot_state_reader.py` for `lot_action_history`.

---

## Common test scaffolding

All dashboard view/action tests follow the pattern in
`tests/apps/dashboard/test_actions.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot, LotActionHistory


def _seed_lot(
    session: AsyncSession,
    *,
    user_action: str | None = None,
    max_bid_cad: Decimal | None = None,
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
    session.add(lot)
    return lot


@pytest.fixture
def _patch_deps(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session
```

Reuse this scaffolding by importing `_seed_lot` and the `_patch_deps`
fixture from `tests/apps/dashboard/test_actions.py` (they are module-level)
or copy where helpful — both are pre-existing patterns in the codebase.

---

# PR 4 — Watchlist kanban

## Task 1 — Extract bucket-builder helper + board partial

Refactor only. No behavior change. Sets up Tasks 2 and 3.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/watched.py`
- Create: `src/carbuyer/apps/dashboard/templates/partials/watchlist_board.html`
- Modify: `src/carbuyer/apps/dashboard/templates/pages/watched.html`
- Test: extend `tests/apps/dashboard/test_views.py` (find existing watched
  page test via `grep -n "watched" tests/apps/dashboard/test_views.py`; if
  none, add a new test function there)

- [ ] **1a. Failing test — helper returns 4 buckets, ordered by close time**

   Add to `tests/apps/dashboard/test_views.py`:

   ```python
   from carbuyer.apps.dashboard.routers.watched import build_watchlist_buckets

   @pytest.mark.asyncio
   async def test_build_watchlist_buckets_groups_by_state(
       _patch_deps: AsyncSession,
   ) -> None:
       session = _patch_deps
       a = Auction(
           source="hibid", source_auction_id="A1", url="https://x",
           canonical_url="https://x", auction_subtype="estate",
           first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
       )
       session.add(a)
       for sa, ua in [
           ("L1", UserAction.INTERESTED),
           ("L2", UserAction.BID_PLACED),
           ("L3", UserAction.PURCHASED),
           ("L4", UserAction.PASSED),
       ]:
           lot = AuctionLot(
               auction=a, source_lot_id=sa, url=f"https://x/{sa}",
               title=sa, user_action=ua,
           )
           if ua == UserAction.BID_PLACED:
               lot.max_bid_cad = Decimal("5000")
           session.add(lot)
       await session.commit()

       buckets = await build_watchlist_buckets(session)
       assert set(buckets) == {
           "interested", "bid_placed", "purchased", "passed",
       }
       assert len(buckets["interested"]) == 1
       assert len(buckets["bid_placed"]) == 1
       assert buckets["interested"][0]["lot"].source_lot_id == "L1"
   ```

- [ ] **1b. Run — fails**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_build_watchlist_buckets_groups_by_state -v`
   Expected: ImportError (`build_watchlist_buckets` not defined).

- [ ] **1c. Implement the helper**

   Edit `src/carbuyer/apps/dashboard/routers/watched.py`. Replace the
   existing `watched` function with:

   ```python
   async def build_watchlist_buckets(
       session: AsyncSession,
   ) -> dict[str, list[dict[str, Any]]]:
       """Group watched lots by user_action, oldest-closing first.

       Per-bucket cap is _PER_BUCKET_LIMIT. Buckets are returned in the
       canonical order Interested → Bid placed → Purchased → Passed so the
       template can iterate dict items if it wants to.
       """
       stmt = (
           select(AuctionLot, Auction)
           .join(Auction, Auction.id == AuctionLot.auction_id)
           .where(AuctionLot.user_action.in_([s.value for s in _BUCKET_STATES]))
           .order_by(Auction.scheduled_end_at.asc().nulls_last())
       )
       rows = (await session.execute(stmt)).all()

       buckets: dict[str, list[dict[str, Any]]] = {
           s.value: [] for s in _BUCKET_STATES
       }
       for lot, auc in rows:
           key = lot.user_action.value if lot.user_action else None
           if key in buckets and len(buckets[key]) < _PER_BUCKET_LIMIT:
               buckets[key].append({"lot": lot, "auction": auc})
       return buckets


   @router.get("/watched", response_class=HTMLResponse)
   async def watched(
       request: Request,
       session: Annotated[AsyncSession, Depends(get_session)],
   ) -> HTMLResponse:
       """4-column kanban over the four user_action states."""
       buckets = await build_watchlist_buckets(session)
       return templates.TemplateResponse(
           request, "pages/watched.html", {"buckets": buckets},
       )
   ```

- [ ] **1d. Run — passes**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_build_watchlist_buckets_groups_by_state -v`
   Expected: PASS.

- [ ] **1e. Failing test — page renders the board wrapper**

   Add to `tests/apps/dashboard/test_views.py`:

   ```python
   @pytest.mark.asyncio
   async def test_watched_renders_watchlist_board(
       _patch_deps: AsyncSession,
   ) -> None:
       session = _patch_deps
       lot = _seed_lot(session, user_action="interested")
       await session.commit()
       _ = lot

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get("/watched")
       assert r.status_code == 200
       assert 'id="watchlist-board"' in r.text
       for label in ("Interested", "Bid placed", "Purchased", "Passed"):
           assert label in r.text
   ```

- [ ] **1f. Run — fails**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_watched_renders_watchlist_board -v`
   Expected: FAIL — `'id="watchlist-board"'` not in response.

- [ ] **1g. Create the board partial**

   Create `src/carbuyer/apps/dashboard/templates/partials/watchlist_board.html`:

   ```jinja
   {# 4-column watchlist board. Included by pages/watched.html and returned
      by /lots/{id}/mark when the click came from the watchlist (HX-Target
      "watchlist-board"). action_target tells nested lot cards to retarget
      their action buttons at the whole board so re-renders relocate the
      card to its new column. #}
   {% set action_target = "#watchlist-board" %}
   <div id="watchlist-board" class="watchlist-board">
     {% for state, label in [
       ("interested", "Interested"),
       ("bid_placed", "Bid placed"),
       ("purchased", "Purchased"),
       ("passed", "Passed"),
     ] %}
       <section class="watchlist-board__column" data-state="{{ state }}">
         <h2 class="watchlist-board__heading">
           {{ label }}
           <span class="watchlist-board__count">{{ buckets[state] | length }}</span>
         </h2>
         {% if buckets[state] %}
           <ul class="watchlist-board__list">
             {% for item in buckets[state] %}
               <li>{% include "partials/lot_card.html" %}</li>
             {% endfor %}
           </ul>
         {% else %}
           <p class="watchlist-board__empty">No lots.</p>
         {% endif %}
       </section>
     {% endfor %}
   </div>
   ```

- [ ] **1h. Replace the page body**

   Replace the content of
   `src/carbuyer/apps/dashboard/templates/pages/watched.html` with:

   ```jinja
   {% extends "base.html" %}
   {% block nav %}watchlist{% endblock %}
   {% block title %}Watchlist — CarBuyer{% endblock %}

   {% block content %}
   <section class="watched">
     <h1>Watchlist</h1>
     {% include "partials/watchlist_board.html" %}
   </section>
   {% endblock %}
   ```

- [ ] **1i. Run — both tests pass**

   Run: `uv run pytest tests/apps/dashboard/test_views.py -v`
   Expected: both new tests PASS, no regression.

- [ ] **1j. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/routers/watched.py \
           src/carbuyer/apps/dashboard/templates/partials/watchlist_board.html \
           src/carbuyer/apps/dashboard/templates/pages/watched.html \
           tests/apps/dashboard/test_views.py
   git commit -m "watchlist: extract board partial + bucket-builder helper"
   ```

---

## Task 2 — `/mark` returns the board when `HX-Target == "watchlist-board"`

This is the wiring that makes button clicks on the kanban re-render the
whole board (so the transitioned card relocates to its new column).

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/actions.py`
- Test: `tests/apps/dashboard/test_actions.py`

- [ ] **2a. Failing test — POST with HX-Target watchlist-board returns the board**

   Add to `tests/apps/dashboard/test_actions.py`:

   ```python
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
       assert r.status_code == 200
       assert 'id="watchlist-board"' in r.text
       # The lot is now in the Passed column, not Interested.
       assert r.text.count("Passed") >= 1
   ```

- [ ] **2b. Run — fails**

   Run: `uv run pytest tests/apps/dashboard/test_actions.py::test_mark_from_watchlist_returns_board_fragment -v`
   Expected: FAIL — response is the lot_card fragment (default branch), no
   `id="watchlist-board"`.

- [ ] **2c. Add the board branch to `/mark`**

   Edit `src/carbuyer/apps/dashboard/routers/actions.py`. Add an import:

   ```python
   from carbuyer.apps.dashboard.routers.watched import build_watchlist_buckets
   ```

   Then, inside `mark_lot`, after the existing `is_button_fragment_target`
   block and before the final `auction = await session.get(...)` line, add:

   ```python
   if hx_target == "watchlist-board":
       buckets = await build_watchlist_buckets(session)
       return templates.TemplateResponse(
           request,
           "partials/watchlist_board.html",
           {"buckets": buckets},
       )
   ```

- [ ] **2d. Run — passes**

   Run: `uv run pytest tests/apps/dashboard/test_actions.py::test_mark_from_watchlist_returns_board_fragment -v`
   Expected: PASS.

- [ ] **2e. Regression check — other branches still work**

   Run: `uv run pytest tests/apps/dashboard/test_actions.py -v`
   Expected: all existing `/mark` tests still PASS.

- [ ] **2f. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/routers/actions.py \
           tests/apps/dashboard/test_actions.py
   git commit -m "actions: /mark returns watchlist board when targeted at it"
   ```

---

## Task 3 — Watchlist lot cards target `#watchlist-board`

Thread an optional `action_target` through `lot_card.html` to
`action_buttons`. The board partial set it in Task 1g; this task wires it
through to the macro call so the kanban cards actually retarget.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/templates/partials/lot_card.html`
- Test: `tests/apps/dashboard/test_views.py`

- [ ] **3a. Failing test — watchlist card buttons retarget the board**

   Add to `tests/apps/dashboard/test_views.py`:

   ```python
   @pytest.mark.asyncio
   async def test_watched_card_actions_target_board(
       _patch_deps: AsyncSession,
   ) -> None:
       session = _patch_deps
       _seed_lot(session, user_action="interested")
       await session.commit()

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get("/watched")
       assert r.status_code == 200
       assert 'hx-target="#watchlist-board"' in r.text
   ```

   Also add a regression guard so other pages keep targeting the card itself:

   ```python
   @pytest.mark.asyncio
   async def test_feed_card_actions_still_target_card(
       _patch_deps: AsyncSession,
   ) -> None:
       """Outside the watchlist, action buttons target the card directly
       (lot-{id}) — this is the pre-existing default and must not regress."""
       session = _patch_deps
       _seed_lot(session)
       await session.commit()

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get("/")
       # The feed renders cards with the default action target.
       assert 'hx-target="#lot-' in r.text
   ```

- [ ] **3b. Run — first fails, second passes**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_watched_card_actions_target_board tests/apps/dashboard/test_views.py::test_feed_card_actions_still_target_card -v`
   Expected: first FAILS (no `hx-target="#watchlist-board"`), second PASSES.

- [ ] **3c. Thread `action_target` through `lot_card.html`**

   Edit `src/carbuyer/apps/dashboard/templates/partials/lot_card.html`,
   line 78 (`{{ action_buttons(lot.id, card_state) }}`). Replace with:

   ```jinja
       {{ action_buttons(lot.id, card_state, target=action_target if action_target is defined else None) }}
   ```

   No other change. `action_target` is set only in `watchlist_board.html`;
   on every other page it is undefined and the macro keeps its default
   `#lot-{lot_id}` target.

- [ ] **3d. Run — both pass**

   Run: `uv run pytest tests/apps/dashboard/test_views.py -v`
   Expected: all PASS.

- [ ] **3e. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/templates/partials/lot_card.html \
           tests/apps/dashboard/test_views.py
   git commit -m "watchlist: cards retarget action buttons at the board"
   ```

---

## Task 4 — Kanban CSS

4-column grid on desktop, horizontal scroll-snap on mobile.

**Files:**
- Create: `src/carbuyer/apps/dashboard/static/css/components/watchlist.css`
- Modify: `src/carbuyer/apps/dashboard/static/css/tailwind.css`

- [ ] **4a. Create the component CSS**

   Create `src/carbuyer/apps/dashboard/static/css/components/watchlist.css`:

   ```css
   /* Watchlist kanban — 4 columns side-by-side on desktop, horizontal
      scroll-snap on mobile (one column at a time, next column peeks). */

   .watchlist-board {
     display: grid;
     gap: var(--space-3, 0.75rem);
     /* Mobile-first: a single horizontally scrolling row. */
     grid-auto-flow: column;
     grid-auto-columns: 85vw;
     overflow-x: auto;
     scroll-snap-type: x mandatory;
     padding-bottom: var(--space-2, 0.5rem);
   }

   .watchlist-board__column {
     scroll-snap-align: start;
     background: var(--color-paper-2);
     border: 1px solid var(--color-rule);
     border-radius: 0.5rem;
     padding: var(--space-2, 0.5rem);
     min-height: 60vh;
     display: flex;
     flex-direction: column;
   }

   .watchlist-board__heading {
     font-family: "Fraunces", serif;
     font-size: 1rem;
     margin: 0 0 var(--space-2, 0.5rem) 0;
     display: flex;
     align-items: center;
     gap: 0.5em;
   }

   .watchlist-board__count {
     font-family: "JetBrains Mono", monospace;
     font-size: 0.8em;
     color: var(--color-muted);
     background: var(--color-paper);
     border-radius: 999px;
     padding: 0.1em 0.6em;
   }

   .watchlist-board__list {
     list-style: none;
     padding: 0;
     margin: 0;
     display: flex;
     flex-direction: column;
     gap: var(--space-2, 0.5rem);
     overflow-y: auto;
     flex: 1;
   }

   .watchlist-board__empty {
     color: var(--color-muted);
     font-style: italic;
     margin: 0;
   }

   /* Desktop: 4-up grid, columns share viewport. */
   @media (min-width: 900px) {
     .watchlist-board {
       grid-auto-flow: row;
       grid-template-columns: repeat(4, 1fr);
       grid-auto-columns: auto;
       overflow-x: visible;
     }
     .watchlist-board__column {
       min-height: 70vh;
     }
   }
   ```

- [ ] **4b. Import the component CSS**

   Edit `src/carbuyer/apps/dashboard/static/css/tailwind.css`. Find the
   block of `@import "./components/*.css";` lines (the other component
   imports — look near where `lot-card.css` is imported). Add a line
   alongside them:

   ```css
   @import "./components/watchlist.css";
   ```

   If no `@import` block for components exists, grep tailwind.css for
   `components/lot-card` and add next to it. (Tailwind v4 picks up
   `@import` of plain CSS files.)

- [ ] **4c. Compile CSS**

   Run: `make css`
   Expected: succeeds; `static/css/app.css` regenerated.

- [ ] **4d. Run test suite**

   Run: `uv run pytest -q`
   Expected: all green.

- [ ] **4e. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/static/css/components/watchlist.css \
           src/carbuyer/apps/dashboard/static/css/tailwind.css \
           src/carbuyer/apps/dashboard/static/css/app.css
   git commit -m "watchlist: kanban CSS — 4-col grid desktop, scroll-snap mobile"
   ```

---

# PR 5 — Place-bid modal

## Task 5 — Add `#modal-slot` to the base layout

Trivial structural change required by every subsequent modal task.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/templates/base.html`
- Test: `tests/apps/dashboard/test_views.py`

- [ ] **5a. Failing test**

   Add to `tests/apps/dashboard/test_views.py`:

   ```python
   @pytest.mark.asyncio
   async def test_base_layout_has_modal_slot(
       _patch_deps: AsyncSession,
   ) -> None:
       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get("/")
       assert r.status_code == 200
       assert 'id="modal-slot"' in r.text
   ```

- [ ] **5b. Run — fails**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_base_layout_has_modal_slot -v`
   Expected: FAIL.

- [ ] **5c. Add the slot**

   Edit `src/carbuyer/apps/dashboard/templates/base.html`. Replace the
   `<main class="container">{% block content %}{% endblock %}</main>` line
   with:

   ```jinja
     <main class="container">{% block content %}{% endblock %}</main>
     <div id="modal-slot"></div>
   ```

- [ ] **5d. Run — passes; commit**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_base_layout_has_modal_slot -v`
   Expected: PASS.

   ```bash
   git add src/carbuyer/apps/dashboard/templates/base.html \
           tests/apps/dashboard/test_views.py
   git commit -m "dashboard: add #modal-slot to base layout"
   ```

---

## Task 6 — `GET /lots/{id}/bid-modal` and `GET /modal/dismiss`

The modal route renders the form; the dismiss route returns empty content
used by the Cancel button and the backdrop.

**Files:**
- Create: `src/carbuyer/apps/dashboard/templates/partials/bid_modal.html`
- Modify: `src/carbuyer/apps/dashboard/routers/actions.py`
- Test: `tests/apps/dashboard/test_actions.py`

- [ ] **6a. Failing tests**

   Add to `tests/apps/dashboard/test_actions.py`:

   ```python
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
       assert r.status_code == 200
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
       )
       await session.commit()

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get(
               f"/lots/{lot.id}/bid-modal?return_target=lot-{lot.id}",
           )
       assert r.status_code == 200
       assert 'value="4200"' in r.text


   @pytest.mark.asyncio
   async def test_modal_dismiss_returns_empty(
       _patch_deps: AsyncSession,
   ) -> None:
       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get("/modal/dismiss")
       assert r.status_code == 200
       assert r.text == ""
   ```

- [ ] **6b. Run — all fail (404)**

   Run: `uv run pytest tests/apps/dashboard/test_actions.py::test_bid_modal_renders_form tests/apps/dashboard/test_actions.py::test_bid_modal_prefills_in_raise_max_mode tests/apps/dashboard/test_actions.py::test_modal_dismiss_returns_empty -v`
   Expected: all FAIL with 404.

- [ ] **6c. Create the modal partial**

   Create `src/carbuyer/apps/dashboard/templates/partials/bid_modal.html`:

   ```jinja
   {# Place-bid modal. CSS-only overlay: visible because it exists in the
      DOM after an HTMX swap into #modal-slot. The Cancel button and
      backdrop are hx-gets that swap empty content back in. #}
   <div class="modal" role="dialog" aria-modal="true" aria-labelledby="bid-modal-title">
     <a class="modal__backdrop"
        hx-get="/modal/dismiss" hx-target="#modal-slot" hx-swap="innerHTML"
        aria-label="Close"></a>
     <div class="modal__panel">
       <h2 id="bid-modal-title" class="modal__title">
         {% if lot.max_bid_cad %}Raise max bid{% else %}Place bid{% endif %}
       </h2>
       <p class="modal__sub t-meta">{{ lot.title or ("Lot #" ~ lot.id) }}</p>
       <form class="modal__form"
             hx-post="/lots/{{ lot.id }}/mark"
             hx-target="#{{ return_target }}"
             hx-swap="outerHTML">
         <input type="hidden" name="action" value="bid_placed">
         <label class="modal__field">
           <span>Your max bid (CAD)</span>
           <input type="number" name="max_bid_cad"
                  min="1" step="1" required
                  {% if lot.max_bid_cad %}value="{{ lot.max_bid_cad|int }}"{% endif %}
                  autofocus>
         </label>
         <div class="modal__actions">
           <a class="modal__cancel"
              hx-get="/modal/dismiss"
              hx-target="#modal-slot" hx-swap="innerHTML">Cancel</a>
           <button type="submit" class="modal__submit">
             {% if lot.max_bid_cad %}Update max{% else %}Place bid{% endif %}
           </button>
         </div>
       </form>
     </div>
   </div>
   ```

- [ ] **6d. Add the routes**

   Edit `src/carbuyer/apps/dashboard/routers/actions.py`. Add at the
   bottom of the file (after `rescore_all`):

   ```python
   @router.get("/lots/{lot_id}/bid-modal", response_class=HTMLResponse)
   async def bid_modal(
       request: Request,
       lot_id: int,
       return_target: str,
       session: Annotated[AsyncSession, Depends(get_session)],
       _user: Annotated[CurrentUser, Depends(current_user)],
   ) -> HTMLResponse:
       """Render the place-bid modal. `return_target` is the element id
       (without leading '#') that the form should swap on submit — the
       caller (action_buttons macro) computes it from its own hx-target so
       the modal posts back into the right region (card / board / fragment).
       """
       lot = await session.get(AuctionLot, lot_id)
       if lot is None:
           raise HTTPException(status_code=404)
       return templates.TemplateResponse(
           request,
           "partials/bid_modal.html",
           {"lot": lot, "return_target": return_target},
       )


   @router.get("/modal/dismiss", response_class=HTMLResponse)
   async def modal_dismiss() -> HTMLResponse:
       """Empty body. Cancel + backdrop swap this into #modal-slot."""
       return HTMLResponse("")
   ```

- [ ] **6e. Run — all pass**

   Run: `uv run pytest tests/apps/dashboard/test_actions.py::test_bid_modal_renders_form tests/apps/dashboard/test_actions.py::test_bid_modal_prefills_in_raise_max_mode tests/apps/dashboard/test_actions.py::test_modal_dismiss_returns_empty -v`
   Expected: all PASS.

- [ ] **6f. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/templates/partials/bid_modal.html \
           src/carbuyer/apps/dashboard/routers/actions.py \
           tests/apps/dashboard/test_actions.py
   git commit -m "actions: GET /bid-modal + GET /modal/dismiss for place-bid modal"
   ```

---

## Task 7 — `action_buttons` macro: bid button opens the modal

Watch and Pass keep their `hx-post` toggle behavior. Bid placed becomes
`hx-get` against `/bid-modal` targeting `#modal-slot`.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/templates/_macros.html`
- Test: extend `tests/apps/dashboard/test_views.py`

- [ ] **7a. Failing test**

   Add to `tests/apps/dashboard/test_views.py`:

   ```python
   @pytest.mark.asyncio
   async def test_bid_button_opens_modal_not_post(
       _patch_deps: AsyncSession,
   ) -> None:
       """The Bid placed button is now an hx-get against the modal route,
       not an hx-post against /mark. Watch and Pass stay as hx-posts."""
       session = _patch_deps
       _seed_lot(session)
       await session.commit()

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get("/")
       assert r.status_code == 200
       # Bid button: hx-get the modal targeting #modal-slot
       assert 'data-action="bid_placed"' in r.text
       assert 'hx-get="/lots/' in r.text and "/bid-modal?return_target=" in r.text
       # Watch + Pass still hx-post /mark
       assert 'data-action="interested"' in r.text
       assert 'hx-post="/lots/' in r.text
   ```

- [ ] **7b. Run — fails**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_bid_button_opens_modal_not_post -v`
   Expected: FAIL — bid button is still hx-post.

- [ ] **7c. Update the macro**

   Edit `src/carbuyer/apps/dashboard/templates/_macros.html`. Replace
   the entire "Bid placed" `<button>` block in the `action_buttons` macro
   (currently around line 145) with:

   ```jinja
     {# Bid placed opens the place-bid modal (which captures max_bid_cad)
        rather than posting /mark directly. The modal posts /mark on
        submit, targeting `return_target` so the originating region
        (card / board / fragment) gets the success swap. `return_target`
        is the id of the macro's hx_target with the leading '#' stripped. #}
     {% set bid_modal_return = hx_target.lstrip('#') %}
     <button class="lot-actions__btn lot-actions__btn--primary" data-action="bid_placed"
             data-active="{{ 'true' if bid_on else 'false' }}"
             hx-get="/lots/{{ lot_id }}/bid-modal?return_target={{ bid_modal_return }}"
             hx-target="#modal-slot" hx-swap="innerHTML"
             aria-label="Place bid / raise max">
       Bid placed
     </button>
   ```

   Keep the Watch and Pass `<button>` blocks unchanged. The wrapper
   `<div class="lot-actions">` still carries `hx-target` and `hx-swap` for
   Watch/Pass; the Bid button now overrides both inline.

- [ ] **7d. Run — passes; regression check**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_bid_button_opens_modal_not_post tests/apps/dashboard/test_actions.py -v`
   Expected: new test PASSES; the existing `test_mark_endpoint_*` tests
   that posted `action=bid_placed` directly **may now FAIL** if any
   posted from a simulated button click — they do not; they post
   directly. All other existing tests should still PASS.

- [ ] **7e. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/templates/_macros.html \
           tests/apps/dashboard/test_views.py
   git commit -m "macros: bid button opens place-bid modal instead of posting directly"
   ```

---

## Task 8 — `/mark` appends OOB `#modal-slot` clear on successful `bid_placed`

When the modal's form submits successfully, the response carries the
normal swap (card or board) **plus** an out-of-band element that empties
`#modal-slot` so the modal disappears in the same round trip.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/actions.py`
- Modify: `src/carbuyer/apps/dashboard/templates/partials/lot_card.html`
- Modify: `src/carbuyer/apps/dashboard/templates/partials/watchlist_board.html`
- Modify: `src/carbuyer/apps/dashboard/templates/partials/action_buttons_fragment.html`
- Test: `tests/apps/dashboard/test_actions.py`

- [ ] **8a. Failing test**

   Add to `tests/apps/dashboard/test_actions.py`:

   ```python
   @pytest.mark.asyncio
   async def test_mark_bid_placed_response_clears_modal_oob(
       _patch_deps: AsyncSession,
   ) -> None:
       """A successful bid_placed transition includes an out-of-band
       element that empties #modal-slot, dismissing the modal."""
       session = _patch_deps
       lot = _seed_lot(session)
       await session.commit()
       lot_id = lot.id

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.post(
               f"/lots/{lot_id}/mark",
               data={"action": "bid_placed", "max_bid_cad": "5000"},
               headers={"HX-Request": "true", "HX-Target": f"lot-{lot_id}"},
           )
       assert r.status_code == 200
       assert 'id="modal-slot"' in r.text
       assert 'hx-swap-oob="true"' in r.text


   @pytest.mark.asyncio
   async def test_mark_non_bid_response_has_no_oob_clear(
       _patch_deps: AsyncSession,
   ) -> None:
       """Watch / Pass / toggle-off responses do not include the OOB
       modal clear — there is no modal to dismiss."""
       session = _patch_deps
       lot = _seed_lot(session)
       await session.commit()
       lot_id = lot.id

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.post(
               f"/lots/{lot_id}/mark",
               data={"action": "interested"},
               headers={"HX-Request": "true", "HX-Target": f"lot-{lot_id}"},
           )
       assert r.status_code == 200
       assert 'hx-swap-oob' not in r.text
   ```

- [ ] **8b. Run — first fails, second passes**

   Run: `uv run pytest tests/apps/dashboard/test_actions.py::test_mark_bid_placed_response_clears_modal_oob tests/apps/dashboard/test_actions.py::test_mark_non_bid_response_has_no_oob_clear -v`
   Expected: first FAILS (no OOB element), second PASSES.

- [ ] **8c. Compute the flag in `/mark`**

   Edit `src/carbuyer/apps/dashboard/routers/actions.py`. Inside
   `mark_lot`, just before the HTMX branch decisions, compute:

   ```python
   include_modal_oob_clear = (
       action == "bid_placed" and not currently_active
   )
   ```

   Then pass `include_modal_oob_clear=include_modal_oob_clear` into the
   three `templates.TemplateResponse(...)` calls (the buttons-fragment
   branch, the watchlist-board branch, and the default lot-card branch).

- [ ] **8d. Render the OOB clear from each fragment**

   Append to the **end** of
   `src/carbuyer/apps/dashboard/templates/partials/lot_card.html` (after
   `</article>`):

   ```jinja
   {% if include_modal_oob_clear %}
   <div id="modal-slot" hx-swap-oob="true"></div>
   {% endif %}
   ```

   Append to the **end** of `partials/watchlist_board.html` (after the
   closing `</div>` of `#watchlist-board`):

   ```jinja
   {% if include_modal_oob_clear %}
   <div id="modal-slot" hx-swap-oob="true"></div>
   {% endif %}
   ```

   Append to the **end** of `partials/action_buttons_fragment.html`
   (after the closing `</div>`):

   ```jinja
   {% if include_modal_oob_clear %}
   <div id="modal-slot" hx-swap-oob="true"></div>
   {% endif %}
   ```

- [ ] **8e. Run — both pass**

   Run: `uv run pytest tests/apps/dashboard/test_actions.py -v`
   Expected: all PASS, no regression.

- [ ] **8f. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/routers/actions.py \
           src/carbuyer/apps/dashboard/templates/partials/lot_card.html \
           src/carbuyer/apps/dashboard/templates/partials/watchlist_board.html \
           src/carbuyer/apps/dashboard/templates/partials/action_buttons_fragment.html \
           tests/apps/dashboard/test_actions.py
   git commit -m "actions: append OOB #modal-slot clear on successful bid_placed"
   ```

---

## Task 9 — Show `max $X` on `bid_placed` lots + modal CSS

Two thin additions: display the captured max bid where lots are shown,
and add the CSS for the modal overlay built in Task 6.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/templates/partials/lot_card.html`
- Modify: `src/carbuyer/apps/dashboard/templates/pages/lot_detail.html`
  (decision-card region)
- Modify: `src/carbuyer/apps/dashboard/static/css/components/watchlist.css`
  (append modal rules — keeping new CSS files to one)
- Test: extend `tests/apps/dashboard/test_views.py` and
  `tests/apps/dashboard/test_lot_detail.py`

- [ ] **9a. Failing test — lot card shows max $X**

   Add to `tests/apps/dashboard/test_views.py`:

   ```python
   @pytest.mark.asyncio
   async def test_lot_card_shows_max_bid_on_bid_placed(
       _patch_deps: AsyncSession,
   ) -> None:
       session = _patch_deps
       _seed_lot(
           session, user_action="bid_placed", max_bid_cad=Decimal("4250"),
       )
       await session.commit()

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get("/watched")
       assert r.status_code == 200
       # money macro wraps the amount in a span, so the literal "max
       # $4,250" doesn't substring-match. Assert the class we add + the
       # rendered amount separately.
       assert "lot-card__max-bid" in r.text
       assert "$4,250" in r.text
   ```

- [ ] **9b. Failing test — lot detail decision card shows max $X**

   Add to `tests/apps/dashboard/test_lot_detail.py` (mirror its existing
   fixture/seed pattern; if absent, copy `_seed_lot` from
   `test_actions.py`):

   ```python
   @pytest.mark.asyncio
   async def test_lot_detail_decision_card_shows_max_bid(
       _patch_deps: AsyncSession,
   ) -> None:
       session = _patch_deps
       lot = _seed_lot(
           session, user_action="bid_placed", max_bid_cad=Decimal("4250"),
       )
       await session.commit()

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get(f"/lots/{lot.id}")
       assert r.status_code == 200
       # money macro wraps the amount in a span — assert class + amount
       # separately rather than a literal cross-span substring.
       assert "decision-card__max-bid" in r.text
       assert "$4,250" in r.text
   ```

- [ ] **9c. Run — both fail**

   Run: `uv run pytest tests/apps/dashboard/test_views.py::test_lot_card_shows_max_bid_on_bid_placed tests/apps/dashboard/test_lot_detail.py::test_lot_detail_decision_card_shows_max_bid -v`
   Expected: both FAIL — string not present.

- [ ] **9d. Render `max $X` in the lot card**

   Edit `src/carbuyer/apps/dashboard/templates/partials/lot_card.html`.
   Inside `<div class="lot-card__price-block">`, after the
   `<div class="lot-card__price">…</div>` line, add:

   ```jinja
         {% if lot.user_action and lot.user_action.value == "bid_placed" and lot.max_bid_cad %}
           <div class="lot-card__max-bid t-meta">max {{ money(lot.max_bid_cad) }}</div>
         {% endif %}
   ```

   (`money` already renders `$X,XXX` with no decimals — the test assertion
   for `"max $4,250"` therefore matches the literal output.)

- [ ] **9e. Render `max $X` in the lot-detail decision card**

   Edit `src/carbuyer/apps/dashboard/templates/pages/lot_detail.html`.
   Locate the `decision-card__price` line (around line 127). After it, add:

   ```jinja
           {% if lot.user_action and lot.user_action.value == "bid_placed" and lot.max_bid_cad %}
             <div class="decision-card__max-bid t-meta">max {{ money(lot.max_bid_cad) }}</div>
           {% endif %}
   ```

- [ ] **9f. Append modal CSS to watchlist.css**

   Append to
   `src/carbuyer/apps/dashboard/static/css/components/watchlist.css`:

   ```css
   /* Place-bid modal — CSS-only overlay. Visible because it exists in the
      DOM after an HTMX swap into #modal-slot; dismissed by swapping
      empty content back in. */
   .modal {
     position: fixed;
     inset: 0;
     z-index: 100;
     display: grid;
     place-items: center;
   }
   .modal__backdrop {
     position: absolute;
     inset: 0;
     background: rgba(20, 17, 13, 0.55);
     cursor: pointer;
   }
   .modal__panel {
     position: relative;
     z-index: 1;
     background: var(--color-paper-3);
     border: 1px solid var(--color-rule-strong);
     border-radius: 0.5rem;
     box-shadow: 0 12px 32px rgba(20, 17, 13, 0.25);
     padding: var(--space-3, 1rem);
     min-width: min(20rem, 90vw);
     max-width: 28rem;
   }
   .modal__title {
     font-family: "Fraunces", serif;
     margin: 0 0 0.25rem 0;
   }
   .modal__sub {
     margin: 0 0 var(--space-2, 0.75rem) 0;
   }
   .modal__form {
     display: flex;
     flex-direction: column;
     gap: var(--space-2, 0.75rem);
   }
   .modal__field {
     display: flex;
     flex-direction: column;
     gap: 0.25rem;
   }
   .modal__field input {
     font-family: "JetBrains Mono", monospace;
     font-size: 1.1rem;
     min-height: 2.5rem;
     padding: 0.4rem 0.6rem;
     border: 1px solid var(--color-rule-strong);
     border-radius: 0.3rem;
     background: var(--color-paper);
   }
   .modal__actions {
     display: flex;
     justify-content: flex-end;
     align-items: center;
     gap: var(--space-2, 0.75rem);
   }
   .modal__cancel {
     color: var(--color-muted);
     cursor: pointer;
     min-height: 2.75rem;
     display: inline-flex;
     align-items: center;
   }
   .modal__submit {
     min-height: 2.75rem;
     padding: 0.5rem 1rem;
     border: 1px solid var(--color-accent-ink);
     background: var(--color-accent);
     color: white;
     border-radius: 0.3rem;
     font-weight: 600;
     cursor: pointer;
   }
   ```

- [ ] **9g. Compile CSS, run tests**

   Run: `make css && uv run pytest -q`
   Expected: green.

- [ ] **9h. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/templates/partials/lot_card.html \
           src/carbuyer/apps/dashboard/templates/pages/lot_detail.html \
           src/carbuyer/apps/dashboard/static/css/components/watchlist.css \
           src/carbuyer/apps/dashboard/static/css/app.css \
           tests/apps/dashboard/test_views.py \
           tests/apps/dashboard/test_lot_detail.py
   git commit -m "lot card + detail: show 'max \$X' on bid_placed lots; modal CSS"
   ```

---

# PR 6 — Lot-detail action history

## Task 10 — `lot_action_history` reader in `db/lot_state.py`

The read helper co-located with `apply_user_action` (the writer for the
same table).

**Files:**
- Modify: `src/carbuyer/db/lot_state.py`
- Create: `tests/db/test_lot_state_reader.py`

- [ ] **10a. Failing test**

   Create `tests/db/test_lot_state_reader.py`:

   ```python
   """Reader for the lot_action_history audit table."""
   from __future__ import annotations

   from datetime import UTC, datetime, timedelta
   from decimal import Decimal

   import pytest
   from sqlalchemy.ext.asyncio import AsyncSession

   from carbuyer.db.enums import UserAction
   from carbuyer.db.lot_state import apply_user_action, lot_action_history
   from carbuyer.db.models import Auction, AuctionLot


   def _seed_lot(session: AsyncSession) -> AuctionLot:
       a = Auction(
           source="hibid", source_auction_id="A1", url="https://x",
           canonical_url="https://x", auction_subtype="estate",
           first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
       )
       session.add(a)
       lot = AuctionLot(
           auction=a, source_lot_id="L1", url="https://x/L1", title="t",
       )
       session.add(lot)
       return lot


   @pytest.mark.asyncio
   async def test_lot_action_history_newest_first(
       session: AsyncSession,
   ) -> None:
       lot = _seed_lot(session)
       await session.flush()
       base = datetime.now(UTC)
       apply_user_action(
           session, lot, UserAction.INTERESTED,
           source="dashboard", now=base,
       )
       apply_user_action(
           session, lot, UserAction.BID_PLACED,
           max_bid_cad=Decimal("3000"),
           source="dashboard", now=base + timedelta(minutes=5),
       )
       apply_user_action(
           session, lot, UserAction.PURCHASED,
           source="dashboard", now=base + timedelta(minutes=10),
       )
       await session.commit()

       rows = await lot_action_history(session, lot.id)
       assert [r.user_action for r in rows] == [
           UserAction.PURCHASED,
           UserAction.BID_PLACED,
           UserAction.INTERESTED,
       ]
       bid_row = rows[1]
       assert bid_row.max_bid_cad == Decimal("3000")
       assert bid_row.source == "dashboard"


   @pytest.mark.asyncio
   async def test_lot_action_history_empty(
       session: AsyncSession,
   ) -> None:
       lot = _seed_lot(session)
       await session.commit()
       rows = await lot_action_history(session, lot.id)
       assert rows == []
   ```

- [ ] **10b. Run — fails**

   Run: `uv run pytest tests/db/test_lot_state_reader.py -v`
   Expected: ImportError (`lot_action_history` not defined).

- [ ] **10c. Implement the reader**

   Edit `src/carbuyer/db/lot_state.py`. Add at the top of the file with
   the other imports:

   ```python
   from collections.abc import Sequence

   from sqlalchemy import select
   from sqlalchemy.ext.asyncio import AsyncSession
   ```

   Add at the bottom of the file:

   ```python
   async def lot_action_history(
       session: AsyncSession,
       lot_id: int,
   ) -> Sequence[LotActionHistory]:
       """Return the audit trail for one lot, newest first.

       Reader co-located with apply_user_action (the writer for the same
       table). Uses the (lot_id, changed_at) index for ordered scan.
       """
       stmt = (
           select(LotActionHistory)
           .where(LotActionHistory.lot_id == lot_id)
           .order_by(LotActionHistory.changed_at.desc())
       )
       result = await session.execute(stmt)
       return result.scalars().all()
   ```

- [ ] **10d. Run — passes**

   Run: `uv run pytest tests/db/test_lot_state_reader.py -v`
   Expected: both PASS.

- [ ] **10e. Commit**

   ```bash
   git add src/carbuyer/db/lot_state.py tests/db/test_lot_state_reader.py
   git commit -m "lot_state: add lot_action_history reader (newest-first)"
   ```

---

## Task 11 — `lot_detail` route + Activity section + CSS

Wire the reader into the page and render the timeline. One vertical
slice — the route change and template section are useless apart and
trivial together.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/lots.py`
- Modify: `src/carbuyer/apps/dashboard/templates/pages/lot_detail.html`
- Modify: `src/carbuyer/apps/dashboard/static/css/components/lot-detail.css`
- Test: extend `tests/apps/dashboard/test_lot_detail.py`

- [ ] **11a. Failing tests — rich rendering + empty state**

   Add to `tests/apps/dashboard/test_lot_detail.py`:

   ```python
   @pytest.mark.asyncio
   async def test_lot_detail_renders_activity_timeline(
       _patch_deps: AsyncSession,
   ) -> None:
       from carbuyer.db.lot_state import apply_user_action

       session = _patch_deps
       lot = _seed_lot(session)
       await session.flush()
       apply_user_action(session, lot, UserAction.INTERESTED, source="dashboard")
       apply_user_action(
           session, lot, UserAction.BID_PLACED,
           max_bid_cad=Decimal("3500"), source="dashboard",
       )
       await session.commit()
       lot_id = lot.id

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get(f"/lots/{lot_id}")
       assert r.status_code == 200
       assert "Activity" in r.text
       # Template renders "bid placed" (underscore replaced with space).
       assert "bid placed" in r.text
       assert "$3,500" in r.text
       assert "dashboard" in r.text


   @pytest.mark.asyncio
   async def test_lot_detail_activity_empty_state(
       _patch_deps: AsyncSession,
   ) -> None:
       session = _patch_deps
       lot = _seed_lot(session)
       await session.commit()
       lot_id = lot.id

       transport = ASGITransport(app=app)
       async with AsyncClient(transport=transport, base_url="http://test") as c:
           r = await c.get(f"/lots/{lot_id}")
       assert r.status_code == 200
       assert "Activity" in r.text
       assert "No recorded activity" in r.text
   ```

- [ ] **11b. Run — both fail**

   Run: `uv run pytest tests/apps/dashboard/test_lot_detail.py::test_lot_detail_renders_activity_timeline tests/apps/dashboard/test_lot_detail.py::test_lot_detail_activity_empty_state -v`
   Expected: both FAIL — Activity section not rendered.

- [ ] **11c. Fetch history in the route**

   Edit `src/carbuyer/apps/dashboard/routers/lots.py`. Add the import at
   the top:

   ```python
   from carbuyer.db.lot_state import lot_action_history
   ```

   In the `lot_detail` handler, after the `auction = ...` fetch and
   before the `TemplateResponse`, add:

   ```python
   history = await lot_action_history(session, lot_id)
   ```

   Update the context dict passed to `TemplateResponse`:

   ```python
   {"lot": lot, "auction": auction, "history": history}
   ```

- [ ] **11d. Add the Activity section**

   Edit `src/carbuyer/apps/dashboard/templates/pages/lot_detail.html`.
   Immediately after the `</section>` closing the Comparable sales block
   (line 117 in the current file) and before `</div>` (line 118 closing
   `lot-detail__main`), insert:

   ```jinja
         {# User action history — every apply_user_action call appends
            a row. Newest first. Empty for lots that predate the audit
            table or were never actioned. #}
         <section class="lot-detail__section lot-detail__activity">
           <h2>Activity</h2>
           {% if history %}
             <ol class="activity-timeline">
               {% for row in history %}
                 <li class="activity-timeline__entry" data-state="{{ row.user_action.value if row.user_action else 'cleared' }}">
                   <time class="activity-timeline__time t-meta">{{ row.changed_at | local_dt }}</time>
                   <span class="activity-timeline__state">
                     {% if row.user_action %}{{ row.user_action.value | replace('_', ' ') }}{% else %}Cleared{% endif %}
                   </span>
                   {% if row.max_bid_cad %}
                     <span class="activity-timeline__bid t-meta">max {{ money(row.max_bid_cad) }}</span>
                   {% endif %}
                   <span class="activity-timeline__source t-meta">via {{ row.source }}</span>
                 </li>
               {% endfor %}
             </ol>
           {% else %}
             <p class="t-meta">No recorded activity.</p>
           {% endif %}
         </section>
   ```

   `money` is already imported at the top of `lot_detail.html`; the
   `local_dt` filter is registered globally in `dashboard/app.py:28`.

   `money(Decimal("3500"))` renders `<span class="money">$3,500</span>`
   (see `_macros.html:6-12`), so the test's `"$3,500"` substring matches.

- [ ] **11e. Add timeline CSS**

   Append to
   `src/carbuyer/apps/dashboard/static/css/components/lot-detail.css`:

   ```css
   /* Activity timeline — audit trail of user_action transitions on the
      lot, rendered newest-first under the lot-detail main column. */
   .lot-detail__activity {
     border-top: 1px solid var(--color-rule);
     padding-top: var(--space-3, 1rem);
   }
   .activity-timeline {
     list-style: none;
     padding: 0;
     margin: 0;
     display: flex;
     flex-direction: column;
     gap: var(--space-2, 0.5rem);
     border-left: 2px solid var(--color-rule);
     padding-left: var(--space-3, 1rem);
   }
   .activity-timeline__entry {
     display: grid;
     grid-template-columns: 9rem auto auto 1fr;
     align-items: baseline;
     gap: 0.75rem;
   }
   .activity-timeline__time {
     font-family: "JetBrains Mono", monospace;
   }
   .activity-timeline__state {
     font-weight: 600;
     text-transform: capitalize;
   }
   .activity-timeline__entry[data-state="bid_placed"] .activity-timeline__state {
     color: var(--color-accent-ink);
   }
   .activity-timeline__entry[data-state="purchased"] .activity-timeline__state {
     color: var(--color-accent-ink);
   }
   .activity-timeline__entry[data-state="passed"] .activity-timeline__state,
   .activity-timeline__entry[data-state="cleared"] .activity-timeline__state {
     color: var(--color-muted);
   }
   .activity-timeline__bid {
     font-family: "JetBrains Mono", monospace;
   }
   .activity-timeline__source {
     justify-self: end;
   }

   @media (max-width: 600px) {
     .activity-timeline__entry {
       grid-template-columns: 1fr auto;
       row-gap: 0.25rem;
     }
     .activity-timeline__source { justify-self: start; grid-column: 1 / -1; }
   }
   ```

- [ ] **11f. Compile CSS + run**

   Run: `make css && uv run pytest -q`
   Expected: all green.

- [ ] **11g. Commit**

   ```bash
   git add src/carbuyer/apps/dashboard/routers/lots.py \
           src/carbuyer/apps/dashboard/templates/pages/lot_detail.html \
           src/carbuyer/apps/dashboard/static/css/components/lot-detail.css \
           src/carbuyer/apps/dashboard/static/css/app.css \
           tests/apps/dashboard/test_lot_detail.py
   git commit -m "lot detail: render activity timeline from lot_action_history"
   ```

---

# Final review

After Task 11:

- [ ] **Full-suite run:** `uv run pytest -q`. Expected: all green.
- [ ] **Type check:** `uv run pyright` (strict). Expected: no new errors.
- [ ] **Lint:** `uv run ruff check .`. Expected: clean.
- [ ] **Manual smoke (optional but encouraged):** start the dashboard,
      open `/watched`, click Bid placed on a card → the modal appears →
      enter a number → submit → the modal vanishes, the card relocates
      to the Bid placed column with `max $X` showing under the price.
      Open the lot's detail page → the Activity section lists the
      transition.
- [ ] **Final review subagent** over the full branch diff vs main.
- [ ] **Memory update:** edit
      `~/.claude/projects/-home-markbohaychuk-repos-CarBuyerAssistant/memory/dashboard-redesign-direction-a.md`
      to mark PRs 4–6 done; trim the "Next PRs: watchlist kanban polish,
      place-bid modal" line.
- [ ] **Open PR.**
