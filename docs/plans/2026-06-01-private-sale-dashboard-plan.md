# Private-Sale Dashboard Integration (PR-3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface private-sale listings in the dashboard — make the saved-search match views polymorphic (auction *and* private matches) and add a new `/private` browse feed with interested/passed actions.

**Architecture:** The `saved_search_matches` table is already polymorphic (`source_kind` + `source_id`, built in PR-2). The saved-search router currently hard-joins `auction_lots`; we extend its count + detail queries to *also* query `private_listings` and merge into a unified, kind-tagged match row. A new self-contained `/private` router renders a best-deals-first feed of `PrivateListing` rows using a richer card, with an interested/passed mark endpoint that mirrors the auction action pattern (minus bidding).

**Tech Stack:** FastAPI + Jinja2 + HTMX, SQLAlchemy 2.0 async, Postgres `carbuyer_test`, pytest (httpx `ASGITransport`).

**UX decisions (locked):** `/private` is a **top-nav** item next to Watchlist; the feed is ordered **best deals first** (`price_deal_score` desc, nulls last); cards are **richer** (thumbnail, deal score, ask vs expected, condition, red/green flag chips, LLM summary, Interested/Pass).

---

## File Structure

```
Modify:
  src/carbuyer/apps/dashboard/routers/searches.py   (polymorphic _match_count/_new_count + search_detail merge)
  src/carbuyer/apps/dashboard/templates/pages/search_detail.html  (render kind-agnostic match/activity rows)
  src/carbuyer/apps/dashboard/templates/_macros.html             (+ private_actions macro)
  src/carbuyer/apps/dashboard/templates/base.html                (+ Private nav link)
  src/carbuyer/apps/dashboard/app.py                             (register private router)

Create:
  src/carbuyer/apps/dashboard/routers/private.py                 (GET /private + POST /private/{id}/mark)
  src/carbuyer/apps/dashboard/templates/pages/private.html       (feed page)
  src/carbuyer/apps/dashboard/templates/partials/private_card.html (richer card + fragment)
  tests/apps/dashboard/test_private.py                           (feed + actions + polymorphic searches)
```

**Conventions to follow (verified in the codebase):**
- Routers import `templates` from `carbuyer.apps.dashboard.app` and deps (`get_session`, `current_user`, `is_htmx`, `CurrentUser`) from `carbuyer.apps.dashboard.deps`.
- `PrivateListing.user_action` is a `UserAction | None` StrEnum column (`SAEnum(..., native_enum=False)`). In queries compare with `UserAction.PASSED.value` and `.is_(None)` (mirrors the auction-side `AuctionLot.user_action` filters). On a loaded instance, `listing.user_action` is a `UserAction` member (StrEnum, so `== "passed"` and `== UserAction.PASSED` both hold).
- Tests use the `_patch_deps` fixture (monkeypatches `deps.get_session_maker` to `session.info["maker"]`) and `_client()` = `AsyncClient(transport=ASGITransport(app=app), base_url="http://test")`. httpx does **not** follow redirects.
- Minimal valid `PrivateListing(source=..., source_listing_id=..., url=..., canonical_url=...)` — all other columns have defaults/server-defaults.
- Magic numbers in test comparisons need `# noqa: PLR2004` (project convention) unless bound to a named constant.

---

## Task 1: Polymorphic match counts (`/searches` badges)

Make the list-page badges (`N matches`, `N new`) count both auction and private matches, excluding passed.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/searches.py:84-123` (`_match_count`, `_new_count`)
- Test: `tests/apps/dashboard/test_private.py`

- [ ] **Step 1: Write the failing test**

Create `tests/apps/dashboard/test_private.py` with the shared harness + the first test:

```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.enums import UserAction
from carbuyer.db.models import (
    AuctionLot,
    Auction,
    PrivateListing,
    SavedSearch,
    SavedSearchMatch,
)


@pytest.fixture
def _patch_deps(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _listing(**kw: object) -> PrivateListing:
    """A minimal valid PrivateListing; override fields via kwargs."""
    defaults: dict[str, object] = {
        "source": "kijiji",
        "source_listing_id": "L1",
        "url": "https://www.kijiji.ca/v-cars-trucks/x/1",
        "canonical_url": "https://www.kijiji.ca/v-cars-trucks/x/1",
    }
    defaults.update(kw)
    return PrivateListing(**defaults)


@pytest.mark.asyncio
async def test_search_badges_count_private_matches(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    listing = _listing(title="Private Mustang", make="Ford", model="Mustang")
    session.add(listing)
    s = SavedSearch(name="stangs", make="Ford")
    session.add_all([listing, s])
    await session.flush()
    session.add(SavedSearchMatch(
        saved_search_id=s.id, source_kind="private_listing", source_id=listing.id,
    ))
    await session.commit()

    async with _client() as client:
        r = await client.get("/searches")
    assert r.status_code == 200  # noqa: PLR2004
    assert "1 match" in r.text  # the private match is counted
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/apps/dashboard/test_private.py::test_search_badges_count_private_matches -v`
Expected: FAIL — badge shows `0 matches` (private matches not counted yet).

- [ ] **Step 3: Make `_match_count` and `_new_count` polymorphic**

In `src/carbuyer/apps/dashboard/routers/searches.py`, add `PrivateListing` to the model import on line 19:

```python
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, SavedSearch, SavedSearchMatch
```

Replace `_match_count` and `_new_count` (lines 84-123) with the version below.

> Two explicit queries (not a shared model-parametrized helper) on purpose:
> `AuctionLot.user_action` and `PrivateListing.user_action` have different
> `Mapped` types (`str | None` vs `UserAction | None`), so a `type[AuctionLot] |
> type[PrivateListing]` helper param would trip pyright strict on the union
> attribute access. Both `where` filters compare against `UserAction.PASSED.value`,
> which works for both column types.

```python
async def _match_count(session: AsyncSession, search_id: int) -> int:
    """Count live (non-dismissed, non-passed) matches of both kinds."""
    auction = (await session.execute(
        select(func.count())
        .select_from(SavedSearchMatch)
        .join(AuctionLot, (AuctionLot.id == SavedSearchMatch.source_id)
              & (SavedSearchMatch.source_kind == "auction_lot"))
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            SavedSearchMatch.dismissed_at.is_(None),
            (AuctionLot.user_action.is_(None))
            | (AuctionLot.user_action != UserAction.PASSED.value),
        )
    )).scalar_one()
    private = (await session.execute(
        select(func.count())
        .select_from(SavedSearchMatch)
        .join(PrivateListing, (PrivateListing.id == SavedSearchMatch.source_id)
              & (SavedSearchMatch.source_kind == "private_listing"))
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            SavedSearchMatch.dismissed_at.is_(None),
            (PrivateListing.user_action.is_(None))
            | (PrivateListing.user_action != UserAction.PASSED.value),
        )
    )).scalar_one()
    return auction + private


async def _new_count(session: AsyncSession, search: SavedSearch) -> int:
    """Live matches of both kinds newer than the last detail-view visit
    (spec's 'N new'), excluding passed so the badge agrees with the detail page."""
    auction = (
        select(func.count())
        .select_from(SavedSearchMatch)
        .join(AuctionLot, (AuctionLot.id == SavedSearchMatch.source_id)
              & (SavedSearchMatch.source_kind == "auction_lot"))
        .where(
            SavedSearchMatch.saved_search_id == search.id,
            SavedSearchMatch.dismissed_at.is_(None),
            (AuctionLot.user_action.is_(None))
            | (AuctionLot.user_action != UserAction.PASSED.value),
        )
    )
    private = (
        select(func.count())
        .select_from(SavedSearchMatch)
        .join(PrivateListing, (PrivateListing.id == SavedSearchMatch.source_id)
              & (SavedSearchMatch.source_kind == "private_listing"))
        .where(
            SavedSearchMatch.saved_search_id == search.id,
            SavedSearchMatch.dismissed_at.is_(None),
            (PrivateListing.user_action.is_(None))
            | (PrivateListing.user_action != UserAction.PASSED.value),
        )
    )
    if search.last_viewed_at is not None:
        auction = auction.where(SavedSearchMatch.matched_at > search.last_viewed_at)
        private = private.where(SavedSearchMatch.matched_at > search.last_viewed_at)
    return (
        (await session.execute(auction)).scalar_one()
        + (await session.execute(private)).scalar_one()
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/apps/dashboard/test_private.py::test_search_badges_count_private_matches -v`
Expected: PASS.

- [ ] **Step 5: Guard against the auction-side regression**

Run the existing saved-search suite to confirm auction badges still work:
Run: `python -m pytest tests/apps/dashboard/test_searches.py -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/searches.py tests/apps/dashboard/test_private.py
git commit -m "feat(dashboard): count private-listing saved-search matches in badges"
```

---

## Task 2: Polymorphic search detail (`/searches/{id}`)

Render private matches alongside auction matches on the detail page, paginated and merged newest-first; passed excluded.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/searches.py:184-245` (`search_detail`)
- Modify: `src/carbuyer/apps/dashboard/templates/pages/search_detail.html:33-70`
- Test: `tests/apps/dashboard/test_private.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/apps/dashboard/test_private.py`:

```python
@pytest.mark.asyncio
async def test_search_detail_renders_private_match(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    shown = _listing(source_listing_id="S1", title="Shown Private Mustang",
                     make="Ford", model="Mustang")
    passed = _listing(source_listing_id="S2", title="Passed Private Mustang",
                      make="Ford", model="Mustang", user_action=UserAction.PASSED)
    s = SavedSearch(name="stangs", make="Ford")
    session.add_all([shown, passed, s])
    await session.flush()
    session.add_all([
        SavedSearchMatch(saved_search_id=s.id, source_kind="private_listing", source_id=shown.id),
        SavedSearchMatch(saved_search_id=s.id, source_kind="private_listing", source_id=passed.id),
    ])
    await session.commit()

    async with _client() as client:
        r = await client.get(f"/searches/{s.id}")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Shown Private Mustang" in r.text
    assert "Passed Private Mustang" not in r.text          # passed excluded
    assert "https://www.kijiji.ca/v-cars-trucks/x/1" in r.text  # links to the external listing
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/apps/dashboard/test_private.py::test_search_detail_renders_private_match -v`
Expected: FAIL — private match not rendered (detail only queries auction_lots).

- [ ] **Step 3: Build unified match rows in `search_detail`**

In `src/carbuyer/apps/dashboard/routers/searches.py`, replace the body of `search_detail` between the `page = max(page, 1)` line and the `# Mark visited` comment (lines 194-233) with:

```python
    page = max(page, 1)

    # ── Current matches (both kinds), merged newest-first, paginated in Python.
    # Match counts per search are modest, so fetching all live matches and
    # slicing is correct and simpler than cross-source SQL pagination.
    auction_rows = (await session.execute(
        select(AuctionLot, Auction, SavedSearchMatch)
        .join(SavedSearchMatch, (SavedSearchMatch.source_kind == "auction_lot")
              & (SavedSearchMatch.source_id == AuctionLot.id))
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            SavedSearchMatch.dismissed_at.is_(None),
            (AuctionLot.user_action.is_(None))
            | (AuctionLot.user_action != UserAction.PASSED.value),
        )
    )).all()
    private_rows = (await session.execute(
        select(PrivateListing, SavedSearchMatch)
        .join(SavedSearchMatch, (SavedSearchMatch.source_kind == "private_listing")
              & (SavedSearchMatch.source_id == PrivateListing.id))
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            SavedSearchMatch.dismissed_at.is_(None),
            (PrivateListing.user_action.is_(None))
            | (PrivateListing.user_action != UserAction.PASSED.value),
        )
    )).all()

    # (matched_at, id, row) tuples — the sort key reads the typed SavedSearchMatch
    # `m`, not the heterogeneous row dict (whose values are `object` to pyright).
    keyed: list[tuple[datetime, int, dict[str, Any]]] = []
    for lot, auc, m in auction_rows:
        keyed.append((m.matched_at, m.id, {
            "kind": "auction_lot", "match": m,
            "title": lot.title or f"Lot #{lot.id}",
            "subtitle": auc.pickup_province or "",
            "detail_url": f"/lots/{lot.id}", "external": False,
        }))
    for listing, m in private_rows:
        keyed.append((m.matched_at, m.id, {
            "kind": "private_listing", "match": m,
            "title": _listing_title(listing),
            "subtitle": listing.pickup_province or "",
            "detail_url": listing.url, "external": True,
        }))
    keyed.sort(key=lambda t: (t[0], t[1]), reverse=True)
    ordered = [row for _, _, row in keyed]
    has_next = len(ordered) > page * _MATCH_PAGE_SIZE
    matches = ordered[(page - 1) * _MATCH_PAGE_SIZE : page * _MATCH_PAGE_SIZE]

    # ── Activity log: every match (incl. dismissed) of both kinds, excl. passed.
    auction_log = (await session.execute(
        select(SavedSearchMatch, AuctionLot.title)
        .join(AuctionLot, (SavedSearchMatch.source_kind == "auction_lot")
              & (SavedSearchMatch.source_id == AuctionLot.id))
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            (AuctionLot.user_action.is_(None))
            | (AuctionLot.user_action != UserAction.PASSED.value),
        )
    )).all()
    private_log = (await session.execute(
        select(SavedSearchMatch, PrivateListing)
        .join(PrivateListing, (SavedSearchMatch.source_kind == "private_listing")
              & (SavedSearchMatch.source_id == PrivateListing.id))
        .where(
            SavedSearchMatch.saved_search_id == search_id,
            (PrivateListing.user_action.is_(None))
            | (PrivateListing.user_action != UserAction.PASSED.value),
        )
    )).all()
    keyed_activity: list[tuple[datetime, int, dict[str, Any]]] = []
    for m, title in auction_log:
        keyed_activity.append((m.matched_at, m.id, {
            "match": m, "title": title or f"Lot #{m.source_id}",
            "detail_url": f"/lots/{m.source_id}", "external": False,
        }))
    for m, listing in private_log:
        keyed_activity.append((m.matched_at, m.id, {
            "match": m, "title": _listing_title(listing),
            "detail_url": listing.url, "external": True,
        }))
    keyed_activity.sort(key=lambda t: (t[0], t[1]), reverse=True)
    activity = [row for _, _, row in keyed_activity[:50]]
```

Update the typing import at the top of the module (line 4) — the merge needs `Any`
(and `datetime` is already imported on line 3):

```python
from typing import Annotated, Any
```

Add this helper near the top of the module (after `_MATCH_PAGE_SIZE = 20` on line 81):

```python
def _listing_title(listing: PrivateListing) -> str:
    parts = [str(p) for p in (listing.year, listing.make, listing.model, listing.trim) if p]
    return " ".join(parts) or listing.title or f"Listing #{listing.id}"
```

> Note: the `select(SavedSearchMatch, AuctionLot.title)` activity query and the surrounding `# Mark visited so the list's "N new" badge resets.` block + `TemplateResponse` (lines 235-245) are unchanged below this replacement — keep them. The `50` magic number for the activity cap already exists in the codebase here.

- [ ] **Step 4: Render kind-agnostic rows in the template**

In `src/carbuyer/apps/dashboard/templates/pages/search_detail.html`, replace the matches `<ul>` block (lines 33-43) with:

```html
  <ul class="search-detail__matches">
    {% for row in matches %}
      <li class="match-row">
        <a href="{{ row.detail_url }}"{% if row.external %} target="_blank" rel="noopener"{% endif %}>{{ row.title }}</a>
        <span class="t-meta">{{ row.subtitle }}{% if row.kind == "private_listing" %} · private{% endif %}</span>
        <button class="btn btn--ghost"
                hx-post="/searches/{{ search.id }}/dismiss/{{ row.match.id }}"
                hx-target="closest .match-row" hx-swap="outerHTML">Dismiss</button>
      </li>
    {% endfor %}
  </ul>
```

And replace the activity `<ol>` block (lines 59-69) with:

```html
  <ol class="search-detail__activity">
    {% for row in activity %}
      <li class="activity-row {% if row.match.dismissed_at %}is-dismissed{% endif %}">
        <time datetime="{{ row.match.matched_at.isoformat() }}">
          {{ row.match.matched_at.strftime("%b %d, %H:%M") }}
        </time>
        <a href="{{ row.detail_url }}"{% if row.external %} target="_blank" rel="noopener"{% endif %}>{{ row.title }}</a>
        {% if row.match.dismissed_at %}<span class="t-meta">dismissed</span>{% endif %}
      </li>
    {% endfor %}
  </ol>
```

- [ ] **Step 5: Run the new + existing detail tests**

Run: `python -m pytest tests/apps/dashboard/test_private.py::test_search_detail_renders_private_match tests/apps/dashboard/test_searches.py -q`
Expected: PASS (all — auction detail rendering still works via the unified rows).

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/searches.py \
        src/carbuyer/apps/dashboard/templates/pages/search_detail.html \
        tests/apps/dashboard/test_private.py
git commit -m "feat(dashboard): render private-listing matches in saved-search detail"
```

---

## Task 3: `/private` browse feed (read-only)

A best-deals-first feed of current private listings, rendered with the richer card. Read-only in this task; the mark action lands in Task 4.

**Files:**
- Create: `src/carbuyer/apps/dashboard/routers/private.py`
- Create: `src/carbuyer/apps/dashboard/templates/pages/private.html`
- Create: `src/carbuyer/apps/dashboard/templates/partials/private_card.html`
- Modify: `src/carbuyer/apps/dashboard/templates/_macros.html` (+ `private_actions`)
- Modify: `src/carbuyer/apps/dashboard/templates/base.html:61` (+ Private nav link)
- Modify: `src/carbuyer/apps/dashboard/app.py:46-66` (register router)
- Test: `tests/apps/dashboard/test_private.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/apps/dashboard/test_private.py`:

```python
@pytest.mark.asyncio
async def test_private_feed_lists_and_excludes_removed_and_passed(
    _patch_deps: AsyncSession,
) -> None:
    session = _patch_deps
    live = _listing(
        source_listing_id="F1", title="Live Jeep", make="Jeep", model="Cherokee",
        year=2016, ask_price_cad=Decimal("13999"), expected_value_cad=Decimal("16500"),
        price_deal_score=0.18, condition_categorical="good",
    )
    removed = _listing(source_listing_id="F2", title="Removed Jeep",
                       removed_at=datetime.now(UTC), price_deal_score=0.5)
    passed = _listing(source_listing_id="F3", title="Passed Jeep",
                      user_action=UserAction.PASSED, price_deal_score=0.9)
    session.add_all([live, removed, passed])
    await session.commit()

    async with _client() as client:
        r = await client.get("/private")
    assert r.status_code == 200  # noqa: PLR2004
    assert "Live Jeep" in r.text or "2016 Jeep Cherokee" in r.text
    assert "Removed Jeep" not in r.text
    assert "Passed Jeep" not in r.text


@pytest.mark.asyncio
async def test_private_feed_orders_best_deal_first(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    weak = _listing(source_listing_id="O1", title="Weak deal", make="A", model="x",
                    price_deal_score=0.05)
    strong = _listing(source_listing_id="O2", title="Strong deal", make="B", model="y",
                      price_deal_score=0.40)
    session.add_all([weak, strong])
    await session.commit()

    async with _client() as client:
        r = await client.get("/private")
    assert r.status_code == 200  # noqa: PLR2004
    assert r.text.index("Strong deal") < r.text.index("Weak deal")


@pytest.mark.asyncio
async def test_private_in_topnav(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.get("/private")
    assert 'href="/private"' in r.text
    assert 'aria-current="page"' in r.text  # Private is the active nav item
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/apps/dashboard/test_private.py::test_private_feed_lists_and_excludes_removed_and_passed -v`
Expected: FAIL — 404 (no `/private` route).

- [ ] **Step 3: Add the `private_actions` macro**

Append to `src/carbuyer/apps/dashboard/templates/_macros.html`:

```html
{# Private-listing action row — Interested / Pass (no bidding). Mirrors the
   toggle pattern of action_buttons: each button posts its own render-time
   `currently_active` literal; clicking an active button clears user_action.
   Targets the whole card (#private-{id}); the mark endpoint returns the
   re-rendered card, or an empty body when the new state is `passed` so the
   card drops out of the best-deals feed. #}
{% macro private_actions(listing_id, current_state) -%}
{% set interested_on = current_state == 'interested' %}
{% set pass_on = current_state == 'passed' %}
<div class="lot-actions" data-size="sm" hx-target="#private-{{ listing_id }}" hx-swap="outerHTML">
  <button class="lot-actions__btn" data-action="interested"
          data-active="{{ 'true' if interested_on else 'false' }}"
          hx-post="/private/{{ listing_id }}/mark"
          hx-vals='{"action":"interested","currently_active":"{{ 'true' if interested_on else 'false' }}"}'
          aria-label="Mark interested">Interested</button>
  <button class="lot-actions__btn" data-action="passed"
          data-active="{{ 'true' if pass_on else 'false' }}"
          hx-post="/private/{{ listing_id }}/mark"
          hx-vals='{"action":"passed","currently_active":"{{ 'true' if pass_on else 'false' }}"}'
          aria-label="Pass on this listing">Pass</button>
</div>
{%- endmacro %}
```

- [ ] **Step 4: Create the richer card partial**

Create `src/carbuyer/apps/dashboard/templates/partials/private_card.html`:

```html
{% from "_macros.html" import money, pct, deal_bucket, state_pill, flag_chip, private_actions %}
{# `listing` is a PrivateListing. Rendered both in the feed (page include) and
   as the HTMX swap target returned by /private/{id}/mark. #}
<article id="private-{{ listing.id }}" class="lot-card private-card"
         data-deal="{{ deal_bucket(listing.price_deal_score) }}">
  {% if listing.photos %}
  <img class="private-card__thumb" src="{{ listing.photos[0] }}" alt=""
       loading="lazy" width="120" height="90">
  {% endif %}
  <div class="private-card__body">
    <div class="private-card__head">
      <a class="private-card__title" href="{{ listing.url }}" target="_blank" rel="noopener">
        {{ [listing.year, listing.make, listing.model, listing.trim] | select | join(" ")
           or listing.title or ("Listing #" ~ listing.id) }}
      </a>
      {% if listing.price_deal_score is not none %}
      <span class="private-card__deal">{{ pct(listing.price_deal_score) }} deal</span>
      {% endif %}
      {{ state_pill(listing.user_action.value if listing.user_action else None) }}
    </div>
    <p class="private-card__pricing">
      Ask {{ money(listing.ask_price_cad) }} · Expected {{ money(listing.expected_value_cad) }}
      {% if listing.condition_categorical %} · {{ listing.condition_categorical }}{% endif %}
    </p>
    {% if listing.green_flags or listing.red_flags %}
    <div class="private-card__flags">
      {% for f in listing.green_flags %}{{ flag_chip(f, "good") }}{% endfor %}
      {% for f in listing.red_flags %}{{ flag_chip(f, "bad") }}{% endfor %}
    </div>
    {% endif %}
    {% if listing.summary %}<p class="private-card__summary">{{ listing.summary }}</p>{% endif %}
    {{ private_actions(listing.id, listing.user_action.value if listing.user_action else None) }}
    <span class="t-meta">{{ listing.pickup_city or "" }}{% if listing.pickup_province %}, {{ listing.pickup_province }}{% endif %}</span>
  </div>
</article>
```

- [ ] **Step 5: Create the feed page**

Create `src/carbuyer/apps/dashboard/templates/pages/private.html`:

```html
{% extends "base.html" %}
{% block nav %}private{% endblock %}
{% block title %}Private listings — CarBuyer{% endblock %}

{% block content %}
<section class="private-feed">
  <h1>Private listings</h1>
  {% if not listings %}
    <p class="t-meta">No private listings yet.</p>
  {% else %}
  <div class="private-feed__list">
    {% for listing in listings %}
      {% include "partials/private_card.html" %}
    {% endfor %}
  </div>
  {% endif %}
</section>
{% endblock %}
```

- [ ] **Step 6: Create the router**

Create `src/carbuyer/apps/dashboard/routers/private.py`:

```python
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.enums import UserAction
from carbuyer.db.models import PrivateListing

router = APIRouter()

# Cap the feed; best deals are first, so the tail is low-value anyway.
_FEED_LIMIT = 100


@router.get("/private", response_class=HTMLResponse)
async def private_feed(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Best-deals-first feed of current (non-removed, non-passed) private listings."""
    stmt = (
        select(PrivateListing)
        .where(
            PrivateListing.removed_at.is_(None),
            (PrivateListing.user_action.is_(None))
            | (PrivateListing.user_action != UserAction.PASSED.value),
        )
        .order_by(
            PrivateListing.price_deal_score.desc().nulls_last(),
            PrivateListing.first_seen_at.desc(),
        )
        .limit(_FEED_LIMIT)
    )
    listings = list((await session.execute(stmt)).scalars().all())
    return templates.TemplateResponse(
        request, "pages/private.html", {"listings": listings},
    )
```

- [ ] **Step 7: Register the router**

In `src/carbuyer/apps/dashboard/app.py`, add `private` to the inner import (line 46-61) and to the `include_router` loop (line 62-65):

```python
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
        private,
        purchases,
        searches,
        sold,
        today,
        watched,
    )
    for router in (
        today, feed, closing, watched, searches, lots, comps, sold,
        purchases, health, actions, needs_plugin, auctions, admin, private,
    ):
        app.include_router(router.router)
```

- [ ] **Step 8: Add the nav link**

In `src/carbuyer/apps/dashboard/templates/base.html`, add the Private link after the Watchlist line (line 61):

```html
    <a href="/watched"   {% if nav == 'watchlist' %}aria-current="page"{% endif %}>Watchlist</a>
    <a href="/private"   {% if nav == 'private'   %}aria-current="page"{% endif %}>Private</a>
```

- [ ] **Step 9: Run the feed tests**

Run: `python -m pytest tests/apps/dashboard/test_private.py -k "feed or topnav" -v`
Expected: PASS (lists live, excludes removed/passed, best-deal-first ordering, Private nav active).

- [ ] **Step 10: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/private.py \
        src/carbuyer/apps/dashboard/templates/pages/private.html \
        src/carbuyer/apps/dashboard/templates/partials/private_card.html \
        src/carbuyer/apps/dashboard/templates/_macros.html \
        src/carbuyer/apps/dashboard/templates/base.html \
        src/carbuyer/apps/dashboard/app.py \
        tests/apps/dashboard/test_private.py
git commit -m "feat(dashboard): add /private best-deals browse feed"
```

---

## Task 4: `/private/{id}/mark` interested/passed action

Toggle `user_action` on a listing; return the re-rendered card, or an empty body when passed (so the card drops out of the feed).

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/private.py`
- Test: `tests/apps/dashboard/test_private.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/apps/dashboard/test_private.py`:

```python
@pytest.mark.asyncio
async def test_mark_interested_sets_action_and_returns_card(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    listing = _listing(source_listing_id="M1", title="Markable", make="Ford", model="F150")
    session.add(listing)
    await session.commit()

    async with _client() as client:
        r = await client.post(
            f"/private/{listing.id}/mark",
            data={"action": "interested", "currently_active": "false"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200  # noqa: PLR2004
    assert f'id="private-{listing.id}"' in r.text  # card fragment returned
    await session.refresh(listing)
    assert listing.user_action == UserAction.INTERESTED


@pytest.mark.asyncio
async def test_mark_passed_returns_empty_so_card_drops(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    listing = _listing(source_listing_id="M2", title="Passable", make="Ford", model="F150")
    session.add(listing)
    await session.commit()

    async with _client() as client:
        r = await client.post(
            f"/private/{listing.id}/mark",
            data={"action": "passed", "currently_active": "false"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200  # noqa: PLR2004
    assert r.text.strip() == ""  # empty body -> HTMX swaps the card away
    await session.refresh(listing)
    assert listing.user_action == UserAction.PASSED


@pytest.mark.asyncio
async def test_mark_toggle_off_clears_action(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    listing = _listing(source_listing_id="M3", title="Toggle", make="Ford", model="F150",
                       user_action=UserAction.INTERESTED)
    session.add(listing)
    await session.commit()

    async with _client() as client:
        r = await client.post(
            f"/private/{listing.id}/mark",
            data={"action": "interested", "currently_active": "true"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200  # noqa: PLR2004
    await session.refresh(listing)
    assert listing.user_action is None


@pytest.mark.asyncio
async def test_mark_unknown_listing_404(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.post(
            "/private/999999/mark",
            data={"action": "interested", "currently_active": "false"},
        )
    assert r.status_code == 404  # noqa: PLR2004
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/apps/dashboard/test_private.py -k mark -v`
Expected: FAIL — 405/404 (no mark route).

- [ ] **Step 3: Implement the mark endpoint**

In `src/carbuyer/apps/dashboard/routers/private.py`, extend the imports and add the endpoint:

```python
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response

from carbuyer.apps.dashboard.deps import CurrentUser, current_user, get_session, is_htmx
```

(Replace the existing `from fastapi import ...` and `from carbuyer.apps.dashboard.deps import get_session` lines accordingly; keep `HTMLResponse`/`select`/`AsyncSession`/`UserAction`/`PrivateListing` imports.)

Add after `private_feed`:

```python
_PRIVATE_ACTIONS = {"interested", "passed"}


@router.post("/private/{listing_id}/mark", response_model=None)
async def mark_private(
    request: Request,
    listing_id: int,
    action: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    currently_active: Annotated[bool, Form()] = False,
) -> HTMLResponse | Response:
    """Set / toggle-off a listing's user_action (interested | passed).

    Returns the re-rendered card on HTMX requests, or an empty body when the
    new state is `passed` so the card drops out of the best-deals feed.
    """
    if action not in _PRIVATE_ACTIONS:
        raise HTTPException(status_code=422, detail=f"invalid action {action!r}")
    listing = await session.get(PrivateListing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404)

    listing.user_action = None if currently_active else UserAction(action)
    await session.commit()
    await session.refresh(listing)

    if not is_htmx(request):
        return Response(status_code=204)
    if listing.user_action == UserAction.PASSED:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "partials/private_card.html", {"listing": listing},
    )
```

- [ ] **Step 4: Run the mark tests**

Run: `python -m pytest tests/apps/dashboard/test_private.py -k mark -v`
Expected: PASS (set, passed-drops, toggle-off, 404).

- [ ] **Step 5: Full dashboard + private suite**

Run: `python -m pytest tests/apps/dashboard/ -q`
Expected: PASS (all — no regression in auction views).

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/private.py tests/apps/dashboard/test_private.py
git commit -m "feat(dashboard): interested/passed actions on /private listings"
```

---

## Final verification (after all tasks)

- [ ] `ruff check src/carbuyer/apps/dashboard tests/apps/dashboard` — clean.
- [ ] `pyright src/carbuyer/apps/dashboard tests/apps/dashboard/test_private.py` — 0 errors.
- [ ] `python -m pytest -q` — full suite green.
- [ ] Manual: `python -m carbuyer.apps.dashboard` (or the project's run command), open `/private` and a `/searches/{id}` that has a private match; confirm the card renders, Interested/Pass work, and a passed card disappears.

## Spec coverage check

| Spec requirement (intake-design §5 + acceptance) | Task |
| --- | --- |
| `/searches/{id}` surfaces `private_listing` matches | Task 2 |
| Match-count + "N new" badges count both kinds | Task 1 |
| `passed` excluded in matches + badges + feed | Tasks 1, 2, 3 |
| `/private` browse page (non-removed, non-passed) | Task 3 |
| Deal score, ask vs expected on the card | Task 3 |
| `interested`/`passed` actions on `/private` | Task 4 |

## Notes / deliberate scope boundaries

- No private **detail** page: private matches in `/searches/{id}` link to the external Kijiji listing (`listing.url`, `target="_blank"`). The spec's dashboard scope is matches-in-searches + the `/private` browse feed only.
- Merged-pagination fetches all live matches per search and slices in Python — correct and simple given realistic per-search match counts. Revisit only if a single search accumulates thousands of live matches.
- New CSS classes (`private-card__*`, `private-feed__*`) reuse the existing `.lot-card` / `.lot-actions` base styling; add minimal rules to `static/css/components/` only if the unstyled layout is unacceptable (out of scope for green tests).
