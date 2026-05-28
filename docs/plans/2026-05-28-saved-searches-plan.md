# Saved Searches Subsystem Implementation Plan (PR-2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add user-defined saved searches: two tables, a source-agnostic pure
matcher, a LISTEN/NOTIFY worker that records matches, and dashboard CRUD + a
match view under a new Watchlist sub-tab.

**Architecture:** A pure `match_listing(MatchableListing, SavedSearch) -> bool`
evaluates AND-combined, NULL-is-wildcard filters with no DB access. An
`adapt_auction_lot(lot, auction)` adapter feeds auction lots into it (private
listings become a second adapter later — no new subsystem). A new
`search_matcher` worker reacts to existing pipeline NOTIFYs plus a new
`saved_search_changed` channel, inserting idempotent rows into
`saved_search_matches`. The dashboard gets a `searches.py` router (list / new /
create / detail / edit / update / dismiss / delete) and a Lots/Searches sub-tab
strip on the Watchlist.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (async), Alembic, psycopg3,
pydantic-settings, FastAPI + Jinja2 + HTMX + Tailwind v4, pytest /
pytest-asyncio, ruff, pyright (strict). Tests run against a real Postgres
(`carbuyer_test`); schema is built from `Base.metadata.create_all()` (NOT
Alembic) so new models appear in tests automatically.

**Spec:** `docs/specs/2026-05-27-notification-pivot-design.md` (PR-2 section).

---

## Decisions that sharpen or deviate from the spec

The spec is the design authority, but four points need a concrete engineering
call. Each is reversible and called out here so a reviewer can object before
code is written.

1. **NOTIFY channels: `valuation_pending` + `notification_pending`, NOT
   `enrichment_pending`.** The spec (§2.3) lists `enrichment_pending` as
   trigger 1 ("new/re-enriched lot"). But in the real pipeline (see
   `src/carbuyer/db/notify.py` channel inventory) `enrichment_pending` is
   emitted by the *ingester* to mean "this lot needs enrichment" — `make`,
   `model`, `year`, `title_status`, `condition_categorical` are all still NULL
   at that moment. Matching there would evaluate every vehicle filter against
   NULLs and surface nothing. The channel that fires once those fields are
   populated is `valuation_pending` (the enricher emits it on completion); the
   channel that fires once `all_in_at_current_bid_cad` + `rarity_score` are
   populated is `notification_pending` (the valuator emits it). The matcher
   therefore listens on **both** (`valuation_pending` for early vehicle-only
   matches, `notification_pending` for price-aware matches), plus the new
   `saved_search_changed`. Because inserts are idempotent (`ON CONFLICT DO
   NOTHING`) running at both points is safe and only ever adds the first-seen
   match.

   These two channels are *also* emitted with an **empty payload** as broadcast
   "wake" signals — `valuator.py` self-notifies `valuation_pending`/
   `notification_pending` with `""` to drain transient leftovers, and
   `/admin/rescore` (`routers/actions.py`) flips every lot to
   `valuation_status=pending` then fires one empty `valuation_pending`. The
   matcher's `_lot_loop` ignores empty payloads (`if not payload: continue`) and
   acts only on per-lot NOTIFYs carrying `str(lot.id)` (the enricher emits these
   on completion; the valuator emits per-lot `notification_pending`). Net for
   spec §2.3 trigger-2 ("price change → re-match"): a *per-lot* re-valuation
   re-drives matching via the per-lot `notification_pending`; a *global*
   `/admin/rescore` does not directly re-drive matching — re-valued lots that
   return to `notification_status=pending` are re-matched via their per-lot
   re-notify, and anything missed is swept by the next startup backfill. This is
   an accepted limitation, not a silent gap.

2. **No SKIP-LOCKED claim; event-driven + idempotent + startup backfill.** The
   spec (§2.3) says "claim pattern matches valuator/notifier." That pattern
   (`carbuyer.db.queue.claim_pending_ids`) flips an `AuctionLot.*_status`
   column to `in_progress`; the matcher has no such per-lot status column and
   adding one is out of scope. Instead the matcher is purely event-driven: each
   NOTIFY payload carries a `lot_id` (or `search_id`), the matcher loads it,
   runs the matcher fn, and inserts with
   `pg_insert(...).on_conflict_do_nothing(...)`. It is single-instance
   (advisory lock) so there is no concurrent claimer to coordinate with. The
   catchup mechanism for NOTIFYs missed during downtime is a **startup
   backfill** (all active searches × all active lots — ~5k evals, <100ms per
   spec §2.8), which replaces the per-worker `_catchup_sweep`.

3. **`user_action='passed'` exclusion is applied at READ time, not at
   match-record time.** Per spec §2.2 "matches are not retracted." `user_action`
   is not a `MatchableListing` field, so `match_listing` never sees it. The
   matcher records a match the first time a lot satisfies the filters
   regardless of user action; the dashboard match view and the future digest
   query filter `user_action != 'passed'` at display time. This keeps the
   matcher source-agnostic (private listings have no `user_action`) and
   consistent with the "not retracted" rule.

4. **systemd unit path is `infra/systemd/`, not `deploy/systemd/`.** The spec
   §2.6 file list says `deploy/systemd/carbuyer-search-matcher.service`; the
   actual directory in this repo is `infra/systemd/` (see
   `infra/systemd/carbuyer-valuator.service`). Use `infra/systemd/`.

Minor, non-deviating choices: `MatchableListing.all_in_cost_cad` is `int`
(spec dataclass), so the adapter rounds the `Decimal`
`all_in_at_current_bid_cad` UP via `math.ceil(...)` — a fractional overrun
(e.g. $30,000.99) must exceed a $30,000 budget cap, so truncation would wrongly
let it match. `SavedSearchMatch` does **not** use
`TimestampMixin` (its `matched_at` is its creation timestamp); `SavedSearch`
does. The `ix_saved_search_matches_active` partial index is keyed
`(saved_search_id, matched_at)` in the default ASC order, not the spec's
`matched_at DESC` — Postgres scans a b-tree backward as cheaply as forward, and
keeping the model and the migration on plain ASC avoids `create_all`-vs-Alembic
schema drift (the test schema comes from the model).

---

## Context the implementer must know

- **Models** live in `src/carbuyer/db/models.py`; all inherit `Base` (and
  usually `TimestampMixin`) from `src/carbuyer/db/base.py`. Columns use
  `Mapped[...] = mapped_column(...)`. Enum-like columns are `String(N)` (not
  native PG enums); `text[]` is `ARRAY(Text)` with
  `server_default=text("'{}'::text[]")`. Money is `Numeric(12, 2)`. PKs are
  `BigInteger`. `on_conflict` uses `index_elements=[...]` (see
  `src/carbuyer/db/upserts.py:121`).
- **Tests use a real Postgres** (`carbuyer_test`) and build schema via
  `Base.metadata.create_all()` in the session-scoped `engine` fixture
  (`tests/conftest.py`). A new model is visible to tests as soon as it is
  imported by `models.py`. The per-test `session` fixture (savepoint isolation)
  rolls back on teardown. `session.info["maker"]` yields a sessionmaker bound
  to the same outer transaction for code that opens its own `get_session()`.
  `asyncio_mode = "auto"` — async tests need no marker, but existing code uses
  `@pytest.mark.asyncio` explicitly; match that.
- **Workers**: package `src/carbuyer/apps/<name>/` with empty `__init__.py`, a
  `__main__.py` that calls `run_worker("<name>", main)` (from
  `carbuyer.apps._runner`), and a module exposing `async def main()`. `main`
  acquires `acquire_singleton_lock("<name>")` (from
  `carbuyer.shared.singleton`), does its catchup, then runs `listen(channel)`
  loops (from `carbuyer.db.notify`), often inside an `asyncio.TaskGroup`. DB
  access: `get_session` / `get_session_maker` from `carbuyer.db.session`.
  Logging: `log = get_logger("<name>")` from `carbuyer.shared.logging`. Channel
  names must match `^[a-z][a-z0-9_]{0,62}$` (so `saved_search_changed` is valid
  with no registration). `notify(session, channel, payload)` dispatches on the
  outer commit — call it before committing.
- **Dashboard**: app factory `create_app()` in
  `src/carbuyer/apps/dashboard/app.py` imports routers *inside* the function
  and calls `app.include_router(router.router)`. A router module exports
  `router: APIRouter`, imports `templates` from
  `carbuyer.apps.dashboard.app` and `get_session` / `current_user` /
  `is_htmx` / `OPEN_STATUSES` from `carbuyer.apps.dashboard.deps`. Full-page
  handlers return `templates.TemplateResponse(request, "pages/X.html", {...})`
  with `response_class=HTMLResponse`. Mutating endpoints depend on
  `current_user`. The modal pattern: `hx-get` a partial into `#modal-slot`;
  `/modal/dismiss` clears it. Tests use
  `AsyncClient(transport=ASGITransport(app=app))` and monkeypatch
  `deps_mod.get_session_maker` to the test maker.
- **CSS**: hand-authored component files in
  `src/carbuyer/apps/dashboard/static/css/components/` are `@import`-ed at the
  bottom of `static/css/tailwind.css`; `make css` compiles to `app.css`. Use
  existing design tokens (`--color-accent`, `--spacing-3`, etc.), never raw hex.
- **Settings**: add a field to `Settings` in
  `src/carbuyer/shared/config.py` as `name: type = default`; env override is
  the upper-cased name. (PR-2 adds one: `search_match_backfill_limit`.)

## File structure

```
Create:
  src/carbuyer/db/saved_searches.py
    - MatchableListing dataclass, adapt_auction_lot(), match_listing() (pure)
  src/carbuyer/apps/search_matcher/__init__.py            (empty)
  src/carbuyer/apps/search_matcher/__main__.py            (run_worker entry)
  src/carbuyer/apps/search_matcher/worker.py              (listen loops + handlers)
  src/carbuyer/apps/dashboard/routers/searches.py         (CRUD + match view)
  src/carbuyer/apps/dashboard/templates/pages/searches_list.html
  src/carbuyer/apps/dashboard/templates/pages/search_detail.html
  src/carbuyer/apps/dashboard/templates/partials/search_form.html
  src/carbuyer/apps/dashboard/templates/partials/search_card.html
  src/carbuyer/apps/dashboard/static/css/components/searches.css
  infra/systemd/carbuyer-search-matcher.service
  alembic/versions/<rev>_saved_searches.py               (hand-written migration)
  tests/db/test_saved_search_matcher.py                  (pure, no DB)
  tests/db/test_saved_search_models.py                   (DB: schema/constraints)
  tests/apps/search_matcher/__init__.py                  (empty)
  tests/apps/search_matcher/test_worker.py               (DB)
  tests/apps/dashboard/test_searches.py                  (DB)

Modify:
  src/carbuyer/db/models.py            (+ SavedSearch, SavedSearchMatch)
  src/carbuyer/shared/config.py        (+ search_match_backfill_limit)
  src/carbuyer/apps/dashboard/app.py   (register searches router)
  src/carbuyer/apps/dashboard/templates/base.html  (no change to topnav; sub-tabs live in page templates)
  src/carbuyer/apps/dashboard/templates/pages/watched.html  (add sub-tab strip)
  src/carbuyer/apps/dashboard/static/css/tailwind.css        (@import searches.css)
```

No change to the 6-item topnav. The Lots/Searches sub-tab strip is rendered
inside the Watchlist page bodies (both keep `{% block nav %}watchlist{% endblock %}`).

---

## Task 1: Data model — `SavedSearch` + `SavedSearchMatch`

**Files:**
- Modify: `src/carbuyer/db/models.py`
- Test: `tests/db/test_saved_search_models.py`
- Create: `alembic/versions/<rev>_saved_searches.py`

- [ ] **Step 1: Write the failing schema test**

Create `tests/db/test_saved_search_models.py`:

```python
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import SavedSearch, SavedSearchMatch


@pytest.mark.asyncio
async def test_saved_search_defaults(session: AsyncSession) -> None:
    s = SavedSearch(name="60s Mustangs", make="Ford", model="Mustang")
    session.add(s)
    await session.flush()
    await session.refresh(s)
    assert s.id is not None
    assert s.is_active is True  # server_default true
    assert s.created_at is not None
    assert s.last_viewed_at is None  # never visited yet
    assert s.year_min is None and s.title_status is None  # NULL = wildcard


@pytest.mark.asyncio
async def test_saved_search_array_columns_roundtrip(session: AsyncSession) -> None:
    s = SavedSearch(
        name="AB/SK clean",
        province=["AB", "SK"],
        title_status=["NORMAL", "REBUILT"],
        condition_categorical=["good", "decent"],
    )
    session.add(s)
    await session.flush()
    await session.refresh(s)
    assert s.province == ["AB", "SK"]
    assert s.title_status == ["NORMAL", "REBUILT"]


@pytest.mark.asyncio
async def test_match_unique_and_cascade(session: AsyncSession) -> None:
    s = SavedSearch(name="x")
    session.add(s)
    await session.flush()

    m = SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=42)
    session.add(m)
    await session.flush()
    await session.refresh(m)
    assert m.matched_at is not None
    assert m.dismissed_at is None

    dup = SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=42)
    session.add(dup)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_match_cascade_on_search_delete(session: AsyncSession) -> None:
    s = SavedSearch(name="x")
    session.add(s)
    await session.flush()
    session.add(SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=1))
    await session.flush()
    await session.delete(s)
    await session.flush()
    remaining = (await session.execute(select(SavedSearchMatch))).scalars().all()
    assert remaining == []
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/db/test_saved_search_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'SavedSearch'`.

- [ ] **Step 3: Add the models**

In `src/carbuyer/db/models.py`, first confirm these names are already imported
at the top (they are used by existing models): `BigInteger`, `Boolean`,
`DateTime`, `ForeignKey`, `Index`, `Integer`, `String`, `Text`,
`UniqueConstraint`, `func`, `text`, `Mapped`, `mapped_column`, `relationship`,
and `ARRAY` from `sqlalchemy.dialects.postgresql`. If any is missing, add it to
the existing import group. Then append these two classes at the end of the file
(after the last model, before any trailing module code):

```python
class SavedSearch(Base, TimestampMixin):
    """A user-defined interest filter. All non-null fields AND together;
    NULL means wildcard. String scalars are case-insensitive; list fields are
    ANY-OF. See carbuyer.db.saved_searches.match_listing for the semantics."""

    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"),
    )

    # Vehicle filters (NULL = wildcard).
    make: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    trim: Mapped[str | None] = mapped_column(String(64))
    year_min: Mapped[int | None] = mapped_column(Integer)
    year_max: Mapped[int | None] = mapped_column(Integer)
    mileage_km_max: Mapped[int | None] = mapped_column(Integer)
    title_status: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    condition_categorical: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    # Location & price filters (NULL = wildcard).
    province: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    max_all_in_cost_cad: Mapped[int | None] = mapped_column(Integer)

    # Stamped each time the detail view is opened; powers the list's
    # "N new since last visit" badge (matches with matched_at > last_viewed_at).
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SavedSearchMatch(Base):
    """A point-in-time fact that a source listing satisfied a saved search.
    Polymorphic (source_kind + source_id) so a future private-sale source needs
    no new table. Never retracted; user mutes via dismissed_at."""

    __tablename__ = "saved_search_matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    saved_search_id: Mapped[int] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="CASCADE"), nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "saved_search_id", "source_kind", "source_id",
            name="uq_saved_search_matches_search_source",
        ),
        Index("ix_saved_search_matches_source", "source_kind", "source_id"),
        Index(
            "ix_saved_search_matches_active",
            "saved_search_id", "matched_at",
            postgresql_where=text("dismissed_at IS NULL"),
        ),
    )
```

(`datetime` and `TimestampMixin` are already imported/defined in `models.py`.)

- [ ] **Step 4: Run it to confirm it passes**

Run: `uv run pytest tests/db/test_saved_search_models.py -q`
Expected: PASS (schema comes from `Base.metadata.create_all()`).

- [ ] **Step 5: Write the production migration**

Generate a revision id and file: `uv run alembic revision -m "saved_searches"`
(do NOT use `--autogenerate`; this repo hand-writes migrations). Open the new
`alembic/versions/<rev>_saved_searches.py` and replace `upgrade`/`downgrade`:

```python
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("make", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("trim", sa.String(length=64), nullable=True),
        sa.Column("year_min", sa.Integer(), nullable=True),
        sa.Column("year_max", sa.Integer(), nullable=True),
        sa.Column("mileage_km_max", sa.Integer(), nullable=True),
        sa.Column("title_status", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("condition_categorical", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("province", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("max_all_in_cost_cad", sa.Integer(), nullable=True),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_searches")),
    )
    op.create_table(
        "saved_search_matches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("saved_search_id", sa.BigInteger(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["saved_search_id"], ["saved_searches.id"],
            name=op.f("fk_saved_search_matches_saved_search_id_saved_searches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_search_matches")),
        sa.UniqueConstraint(
            "saved_search_id", "source_kind", "source_id",
            name="uq_saved_search_matches_search_source",
        ),
    )
    op.create_index(
        "ix_saved_search_matches_source", "saved_search_matches",
        ["source_kind", "source_id"], unique=False,
    )
    op.create_index(
        "ix_saved_search_matches_active", "saved_search_matches",
        ["saved_search_id", "matched_at"], unique=False,
        postgresql_where=sa.text("dismissed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_saved_search_matches_active", table_name="saved_search_matches")
    op.drop_index("ix_saved_search_matches_source", table_name="saved_search_matches")
    op.drop_table("saved_search_matches")
    op.drop_table("saved_searches")
```

Set `down_revision` to the current head (find it with
`uv run alembic heads`; it is the most recent revision id in
`alembic/versions/`). Verify the migration applies cleanly against a scratch
DB if available: `uv run alembic upgrade head` then `uv run alembic downgrade -1`.
(If no DB is wired for migration smoke-testing locally, at minimum confirm the
file imports without error: `uv run python -c "import alembic.versions.<rev>_saved_searches"`.)

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/db/models.py tests/db/test_saved_search_models.py alembic/versions/
git commit -m "feat(db): add saved_searches + saved_search_matches tables"
```

---

## Task 2: Pure matcher — `MatchableListing` + `adapt_auction_lot` + `match_listing`

**Files:**
- Create: `src/carbuyer/db/saved_searches.py`
- Test: `tests/db/test_saved_search_matcher.py`

This task is pure (no DB). TDD applies cleanly; tests instantiate `SavedSearch`
in memory (unset columns are `None` = wildcard) and `MatchableListing`
directly.

- [ ] **Step 1: Write the failing matcher tests**

Create `tests/db/test_saved_search_matcher.py`:

```python
from __future__ import annotations

from carbuyer.db.models import SavedSearch
from carbuyer.db.saved_searches import MatchableListing, match_listing


def _listing(**overrides: object) -> MatchableListing:
    base: dict[str, object] = dict(
        source_kind="auction_lot", source_id=1,
        make="Ford", model="Mustang", year=1968, trim="Fastback GT",
        mileage_km=90_000, title_status="NORMAL",
        condition_categorical="good", province="AB",
        all_in_cost_cad=25_000, rarity_score=2.1,
    )
    base.update(overrides)
    return MatchableListing(**base)  # type: ignore[arg-type]


def test_empty_search_matches_everything() -> None:
    assert match_listing(_listing(), SavedSearch(name="all")) is True


def test_make_case_insensitive_eq() -> None:
    assert match_listing(_listing(make="ford"), SavedSearch(name="x", make="FORD")) is True
    assert match_listing(_listing(make="Toyota"), SavedSearch(name="x", make="Ford")) is False


def test_make_null_listing_fails_when_filter_set() -> None:
    assert match_listing(_listing(make=None), SavedSearch(name="x", make="Ford")) is False


def test_model_case_insensitive_eq() -> None:
    assert match_listing(_listing(model="MUSTANG"), SavedSearch(name="x", model="mustang")) is True
    assert match_listing(_listing(model="Camaro"), SavedSearch(name="x", model="Mustang")) is False


def test_trim_substring_case_insensitive() -> None:
    # trim is a contains-match, not equality.
    assert match_listing(_listing(trim="Fastback GT"), SavedSearch(name="x", trim="gt")) is True
    assert match_listing(_listing(trim="Coupe"), SavedSearch(name="x", trim="gt")) is False
    assert match_listing(_listing(trim=None), SavedSearch(name="x", trim="gt")) is False


def test_year_range_inclusive_both_bounds() -> None:
    s = SavedSearch(name="x", year_min=1965, year_max=1970)
    assert match_listing(_listing(year=1965), s) is True
    assert match_listing(_listing(year=1970), s) is True
    assert match_listing(_listing(year=1964), s) is False
    assert match_listing(_listing(year=1971), s) is False


def test_year_open_bounds() -> None:
    assert match_listing(_listing(year=1990), SavedSearch(name="x", year_min=1980)) is True
    assert match_listing(_listing(year=1975), SavedSearch(name="x", year_min=1980)) is False
    assert match_listing(_listing(year=1975), SavedSearch(name="x", year_max=1980)) is True
    assert match_listing(_listing(year=None), SavedSearch(name="x", year_min=1980)) is False


def test_mileage_max_inclusive() -> None:
    s = SavedSearch(name="x", mileage_km_max=100_000)
    assert match_listing(_listing(mileage_km=100_000), s) is True
    assert match_listing(_listing(mileage_km=100_001), s) is False
    assert match_listing(_listing(mileage_km=None), s) is False


def test_title_status_any_of_case_insensitive() -> None:
    s = SavedSearch(name="x", title_status=["NORMAL", "REBUILT"])
    assert match_listing(_listing(title_status="normal"), s) is True
    assert match_listing(_listing(title_status="SALVAGE"), s) is False
    assert match_listing(_listing(title_status=None), s) is False


def test_condition_any_of() -> None:
    s = SavedSearch(name="x", condition_categorical=["good", "excellent"])
    assert match_listing(_listing(condition_categorical="good"), s) is True
    assert match_listing(_listing(condition_categorical="rough"), s) is False


def test_province_any_of_case_insensitive() -> None:
    s = SavedSearch(name="x", province=["AB", "SK"])
    assert match_listing(_listing(province="ab"), s) is True
    assert match_listing(_listing(province="MB"), s) is False
    assert match_listing(_listing(province=None), s) is False


def test_max_all_in_cost_inclusive() -> None:
    s = SavedSearch(name="x", max_all_in_cost_cad=30_000)
    assert match_listing(_listing(all_in_cost_cad=30_000), s) is True
    assert match_listing(_listing(all_in_cost_cad=30_001), s) is False
    assert match_listing(_listing(all_in_cost_cad=None), s) is False


def test_empty_list_filter_is_treated_as_wildcard() -> None:
    # A persisted empty array (no options chosen) must not exclude everything.
    assert match_listing(_listing(province="AB"), SavedSearch(name="x", province=[])) is True


def test_all_filters_and_together() -> None:
    s = SavedSearch(
        name="dream", make="Ford", model="Mustang",
        year_min=1965, year_max=1970, mileage_km_max=120_000,
        title_status=["NORMAL"], province=["AB"], max_all_in_cost_cad=40_000,
    )
    assert match_listing(_listing(), s) is True
    # one field off → no match
    assert match_listing(_listing(province="BC"), s) is False
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/db/test_saved_search_matcher.py -q`
Expected: FAIL — `ModuleNotFoundError: carbuyer.db.saved_searches`.

- [ ] **Step 3: Implement the matcher**

Create `src/carbuyer/db/saved_searches.py`:

```python
"""Source-agnostic saved-search matching.

`match_listing` is a pure predicate over a `MatchableListing` (a flattened,
DB-free view of a candidate) and a `SavedSearch` (the ORM filter row). All
non-null filters AND together; NULL/empty filters are wildcards. String scalars
compare case-insensitively; `trim` is a substring match; list filters are
ANY-OF; year is a closed range; mileage and cost are inclusive caps. A NULL
listing field never satisfies a set filter.

`adapt_auction_lot` builds a `MatchableListing` from an auction lot + its
auction. A future private-sale source adds its own adapter here — the matcher
and every downstream read path stay unchanged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from carbuyer.db.models import Auction, AuctionLot, SavedSearch


@dataclass(frozen=True, slots=True)
class MatchableListing:
    source_kind: str
    source_id: int
    make: str | None
    model: str | None
    year: int | None
    trim: str | None
    mileage_km: int | None
    title_status: str | None
    condition_categorical: str | None
    province: str | None
    all_in_cost_cad: int | None
    rarity_score: float | None


def adapt_auction_lot(lot: AuctionLot, auction: Auction) -> MatchableListing:
    all_in = lot.all_in_at_current_bid_cad
    return MatchableListing(
        source_kind="auction_lot",
        source_id=lot.id,
        make=lot.make,
        model=lot.model,
        year=lot.year,
        trim=lot.trim,
        mileage_km=lot.mileage_km,
        title_status=lot.title_status,
        condition_categorical=lot.condition_categorical,
        province=auction.pickup_province,
        all_in_cost_cad=math.ceil(all_in) if all_in is not None else None,
        rarity_score=lot.rarity_score,
    )


def _eq_ci(value: str | None, want: str | None) -> bool:
    if want is None:
        return True
    return value is not None and value.casefold() == want.casefold()


def _contains_ci(value: str | None, want: str | None) -> bool:
    if want is None:
        return True
    return value is not None and want.casefold() in value.casefold()


def _any_of_ci(value: str | None, options: list[str] | None) -> bool:
    if not options:  # None or empty list → wildcard
        return True
    return value is not None and any(value.casefold() == o.casefold() for o in options)


def match_listing(listing: MatchableListing, search: SavedSearch) -> bool:
    if not _eq_ci(listing.make, search.make):
        return False
    if not _eq_ci(listing.model, search.model):
        return False
    if not _contains_ci(listing.trim, search.trim):
        return False
    if search.year_min is not None and (listing.year is None or listing.year < search.year_min):
        return False
    if search.year_max is not None and (listing.year is None or listing.year > search.year_max):
        return False
    if search.mileage_km_max is not None and (
        listing.mileage_km is None or listing.mileage_km > search.mileage_km_max
    ):
        return False
    if not _any_of_ci(listing.title_status, search.title_status):
        return False
    if not _any_of_ci(listing.condition_categorical, search.condition_categorical):
        return False
    if not _any_of_ci(listing.province, search.province):
        return False
    if search.max_all_in_cost_cad is not None and (
        listing.all_in_cost_cad is None or listing.all_in_cost_cad > search.max_all_in_cost_cad
    ):
        return False
    return True
```

- [ ] **Step 4: Run it to confirm it passes**

Run: `uv run pytest tests/db/test_saved_search_matcher.py -q`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/db/saved_searches.py tests/db/test_saved_search_matcher.py
git commit -m "feat(db): source-agnostic saved-search matcher + auction-lot adapter"
```

---

## Task 3: `search_matcher` worker

**Files:**
- Create: `src/carbuyer/apps/search_matcher/__init__.py` (empty),
  `__main__.py`, `worker.py`
- Create: `infra/systemd/carbuyer-search-matcher.service`
- Modify: `src/carbuyer/shared/config.py`
- Test: `tests/apps/search_matcher/__init__.py` (empty),
  `tests/apps/search_matcher/test_worker.py`

- [ ] **Step 1: Add the backfill-limit setting**

In `src/carbuyer/shared/config.py`, add (near the other worker tunables, e.g.
after `valuation_batch_size`):

```python
    search_match_backfill_limit: int = 20_000
```

- [ ] **Step 2: Write the failing worker tests**

Create empty `tests/apps/search_matcher/__init__.py`, then
`tests/apps/search_matcher/test_worker.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.search_matcher import worker as worker_mod
from carbuyer.apps.search_matcher.worker import (  # pyright: ignore[reportPrivateUsage]
    process_lot,
    process_search,
    startup_backfill,
)
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch


def _seed_auction(session: AsyncSession, *, province: str = "AB") -> Auction:
    a = Auction(
        source="test", source_auction_id="A1", url="https://x",
        canonical_url="https://x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        pickup_province=province,
    )
    session.add(a)
    return a


def _seed_lot(
    session: AsyncSession, auction: Auction, *,
    source_lot_id: str = "L1", make: str | None = "Ford",
    model: str | None = "Mustang", year: int | None = 1968,
    title_status: str = "NORMAL", lot_status: str = "open",
    all_in: Decimal | None = Decimal("25000"),
) -> AuctionLot:
    lot = AuctionLot(
        auction=auction, source_lot_id=source_lot_id,
        url=f"https://x/{source_lot_id}", title="car",
        make=make, model=model, year=year, title_status=title_status,
        lot_status=lot_status, all_in_at_current_bid_cad=all_in,
    )
    session.add(lot)
    return lot


@pytest.fixture
def _patched_get_session(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(worker_mod, "get_session", fake_get_session)
    return session


async def _matches(session: AsyncSession, search_id: int) -> list[int]:
    rows = (await session.execute(
        select(SavedSearchMatch.source_id)
        .where(SavedSearchMatch.saved_search_id == search_id)
    )).scalars().all()
    return sorted(rows)


@pytest.mark.asyncio
async def test_process_lot_records_match_for_active_search(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    lot = _seed_lot(session, a)
    s = SavedSearch(name="stangs", make="Ford", model="Mustang")
    session.add(s)
    await session.flush()

    n = await process_lot(lot.id)
    assert n == 1
    assert await _matches(session, s.id) == [lot.id]


@pytest.mark.asyncio
async def test_process_lot_is_idempotent(_patched_get_session: AsyncSession) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    lot = _seed_lot(session, a)
    s = SavedSearch(name="stangs", make="Ford")
    session.add(s)
    await session.flush()

    assert await process_lot(lot.id) == 1
    assert await process_lot(lot.id) == 1  # ON CONFLICT DO NOTHING
    assert await _matches(session, s.id) == [lot.id]


@pytest.mark.asyncio
async def test_process_lot_skips_inactive_search(_patched_get_session: AsyncSession) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    lot = _seed_lot(session, a)
    s = SavedSearch(name="off", make="Ford", is_active=False)
    session.add(s)
    await session.flush()
    assert await process_lot(lot.id) == 0
    assert await _matches(session, s.id) == []


@pytest.mark.asyncio
async def test_process_lot_missing_returns_zero(_patched_get_session: AsyncSession) -> None:
    assert await process_lot(999_999) == 0


@pytest.mark.asyncio
async def test_process_search_backfills_active_lots_only(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    open_lot = _seed_lot(session, a, source_lot_id="L1", lot_status="open")
    closed_lot = _seed_lot(session, a, source_lot_id="L2", lot_status="closed")
    s = SavedSearch(name="stangs", make="Ford")
    session.add(s)
    await session.flush()

    n = await process_search(s.id)
    assert n == 1
    assert await _matches(session, s.id) == [open_lot.id]
    assert closed_lot.id not in await _matches(session, s.id)


@pytest.mark.asyncio
async def test_startup_backfill_matches_cross_product(
    _patched_get_session: AsyncSession,
) -> None:
    session = _patched_get_session
    a = _seed_auction(session)
    await session.flush()
    ford = _seed_lot(session, a, source_lot_id="L1", make="Ford")
    toyota = _seed_lot(session, a, source_lot_id="L2", make="Toyota")
    s_ford = SavedSearch(name="ford", make="Ford")
    s_any = SavedSearch(name="any")
    session.add_all([s_ford, s_any])
    await session.flush()

    await startup_backfill()
    assert await _matches(session, s_ford.id) == [ford.id]
    assert await _matches(session, s_any.id) == sorted([ford.id, toyota.id])
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `uv run pytest tests/apps/search_matcher/test_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: carbuyer.apps.search_matcher.worker`.

- [ ] **Step 4: Implement `worker.py`**

Create `src/carbuyer/apps/search_matcher/worker.py`:

```python
"""Saved-search matcher worker.

Single-instance LISTEN/NOTIFY worker. On a per-lot NOTIFY (valuation_pending or
notification_pending carrying str(lot.id)) it matches that lot against all
active searches. These channels are also emitted with an empty payload as
broadcast wakes (valuator self-notify, /admin/rescore); those are ignored —
per-lot re-notifies plus the startup backfill cover them (see plan Decision 1).
On saved_search_changed (carries a search_id) it backfills that one search
against all active lots. At startup it backfills the full cross product to
recover NOTIFYs missed while down. Inserts are idempotent
(ON CONFLICT DO NOTHING), so re-running any handler only ever adds first-seen
matches; matches are never retracted (spec §2.2)."""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import LotStatus
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch
from carbuyer.db.notify import listen
from carbuyer.db.saved_searches import MatchableListing, adapt_auction_lot, match_listing
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock

log = get_logger("search_matcher")

# A lot is matchable while it is still biddable. Mirrors dashboard
# OPEN_STATUSES but defined here to avoid importing the dashboard from a worker.
_ACTIVE_LOT_STATUSES: tuple[str, ...] = (
    LotStatus.OPEN.value,
    LotStatus.CLOSING_SOON.value,
    LotStatus.EXTENDED.value,
)

# Lot-data channels the matcher reacts to. valuation_pending fires once the
# enricher has populated vehicle fields; notification_pending fires once the
# valuator has populated all_in_at_current_bid_cad + rarity_score. See the PR-2
# plan "Decisions" section for why enrichment_pending is deliberately excluded.
_LOT_CHANNELS: tuple[str, ...] = ("valuation_pending", "notification_pending")
_SEARCH_CHANNEL = "saved_search_changed"


async def _active_searches(session: AsyncSession) -> list[SavedSearch]:
    stmt = select(SavedSearch).where(SavedSearch.is_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def _active_listings(session: AsyncSession, *, limit: int) -> list[MatchableListing]:
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.lot_status.in_(_ACTIVE_LOT_STATUSES))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [adapt_auction_lot(lot, auction) for lot, auction in rows]


async def _insert_matches(session: AsyncSession, triples: list[tuple[int, str, int]]) -> None:
    if not triples:
        return
    values = [
        {"saved_search_id": sid, "source_kind": kind, "source_id": sid_src}
        for sid, kind, sid_src in triples
    ]
    stmt = pg_insert(SavedSearchMatch).values(values).on_conflict_do_nothing(
        index_elements=["saved_search_id", "source_kind", "source_id"],
    )
    await session.execute(stmt)


async def process_lot(lot_id: int) -> int:
    """Match one lot against all active searches. Returns the number of
    (search, lot) pairs that matched (pre-dedup)."""
    async with get_session() as s, s.begin():
        pair = (await s.execute(
            select(AuctionLot, Auction)
            .join(Auction, Auction.id == AuctionLot.auction_id)
            .where(AuctionLot.id == lot_id)
        )).first()
        if pair is None:
            return 0
        lot, auction = pair
        listing = adapt_auction_lot(lot, auction)
        searches = await _active_searches(s)
        hits = [sch for sch in searches if match_listing(listing, sch)]
        await _insert_matches(
            s, [(sch.id, listing.source_kind, listing.source_id) for sch in hits],
        )
        return len(hits)


async def process_search(search_id: int) -> int:
    """Backfill one search against all active lots. Returns matched-lot count."""
    async with get_session() as s, s.begin():
        search = await s.get(SavedSearch, search_id)
        if search is None or not search.is_active:
            return 0
        listings = await _active_listings(s, limit=settings.search_match_backfill_limit)
        hits = [lst for lst in listings if match_listing(lst, search)]
        await _insert_matches(
            s, [(search.id, lst.source_kind, lst.source_id) for lst in hits],
        )
        return len(hits)


async def startup_backfill() -> int:
    """Match the full active-search × active-lot cross product. Catchup for
    NOTIFYs missed while the worker was down. Returns matched-pair count."""
    async with get_session() as s, s.begin():
        searches = await _active_searches(s)
        listings = await _active_listings(s, limit=settings.search_match_backfill_limit)
        triples: list[tuple[int, str, int]] = [
            (sch.id, lst.source_kind, lst.source_id)
            for sch in searches
            for lst in listings
            if match_listing(lst, sch)
        ]
        await _insert_matches(s, triples)
        return len(triples)


async def _lot_loop(channel: str) -> None:
    async for payload in listen(channel):
        if not payload:
            continue
        try:
            await process_lot(int(payload))
        except Exception:
            log.exception("lot match failed; sleeping", channel=channel, payload=payload)
            await asyncio.sleep(5)


async def _search_loop() -> None:
    async for payload in listen(_SEARCH_CHANNEL):
        if not payload:
            continue
        try:
            await process_search(int(payload))
        except Exception:
            log.exception("search backfill failed; sleeping", payload=payload)
            await asyncio.sleep(5)


async def main() -> None:
    lock_conn = await acquire_singleton_lock("search_matcher")
    try:
        matched = await startup_backfill()
        log.info("startup backfill complete", matched_pairs=matched)
        async with asyncio.TaskGroup() as tg:
            for ch in _LOT_CHANNELS:
                tg.create_task(_lot_loop(ch), name=f"lot_loop:{ch}")
            tg.create_task(_search_loop(), name="search_loop")
    finally:
        await lock_conn.close()
```

- [ ] **Step 5: Implement `__main__.py`**

Create `src/carbuyer/apps/search_matcher/__main__.py`:

```python
from carbuyer.apps._runner import run_worker
from carbuyer.apps.search_matcher.worker import main

if __name__ == "__main__":
    run_worker("search_matcher", main)
```

And create empty `src/carbuyer/apps/search_matcher/__init__.py`.

- [ ] **Step 6: Run the worker tests**

Run: `uv run pytest tests/apps/search_matcher/test_worker.py -q`
Expected: PASS.

- [ ] **Step 7: Add the systemd unit**

Create `infra/systemd/carbuyer-search-matcher.service` (copy
`infra/systemd/carbuyer-valuator.service`, change Description + ExecStart):

```ini
[Unit]
Description=CarBuyer saved-search matcher worker
After=network-online.target carbuyer-postgres.service
Requires=carbuyer-postgres.service

[Service]
Type=simple
User=markbohaychuk
WorkingDirectory=/home/markbohaychuk/repos/CarBuyerAssistant
EnvironmentFile=-/home/markbohaychuk/repos/CarBuyerAssistant/.env
ExecStart=/home/markbohaychuk/repos/CarBuyerAssistant/.venv/bin/python -m carbuyer.apps.search_matcher
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictSUIDSGID=true
RestrictRealtime=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/home/markbohaychuk/repos/CarBuyerAssistant

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 8: Commit**

```bash
git add src/carbuyer/apps/search_matcher/ tests/apps/search_matcher/ \
        src/carbuyer/shared/config.py infra/systemd/carbuyer-search-matcher.service
git commit -m "feat(search_matcher): LISTEN/NOTIFY worker recording saved-search matches"
```

---

## Task 4: Dashboard CRUD + match view

**Files:**
- Create: `src/carbuyer/apps/dashboard/routers/searches.py`
- Create: templates `pages/searches_list.html`, `pages/search_detail.html`,
  `partials/search_form.html`, `partials/search_card.html`
- Create: `src/carbuyer/apps/dashboard/static/css/components/searches.css`
- Modify: `src/carbuyer/apps/dashboard/app.py`,
  `src/carbuyer/apps/dashboard/static/css/tailwind.css`
- Test: `tests/apps/dashboard/test_searches.py`

The router covers the spec §2.4 endpoints. List/detail are full pages; the new
form opens in `#modal-slot`; create/update emit `saved_search_changed` so the
matcher backfills. Forms send multi-value selects (`title_status`,
`condition_categorical`, `province`) and comma-free scalar inputs.

- [ ] **Step 1: Write the failing router tests**

Create `tests/apps/dashboard/test_searches.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.apps.dashboard.routers import searches as searches_mod
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch


@pytest.fixture
def _patch_deps(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_create_persists_row_and_notifies(
    _patch_deps: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patch_deps
    sent: list[tuple[str, str]] = []

    async def fake_notify(_s: object, channel: str, payload: str = "") -> None:
        sent.append((channel, payload))

    # The router imports notify into its own module namespace, so patch it there.
    monkeypatch.setattr(searches_mod, "notify", fake_notify)

    async with _client() as client:
        r = await client.post("/searches", data={
            "name": "60s Mustangs", "make": "Ford", "model": "Mustang",
            "year_min": "1965", "year_max": "1970",
            "title_status": ["NORMAL"], "province": ["AB", "SK"],
        })
    # httpx does not follow redirects by default; create returns a 303.
    assert r.status_code == 303

    rows = (await session.execute(select(SavedSearch))).scalars().all()
    assert len(rows) == 1
    assert rows[0].make == "Ford"
    assert rows[0].province == ["AB", "SK"]
    assert any(ch == "saved_search_changed" for ch, _ in sent)


@pytest.mark.asyncio
async def test_list_renders_cards(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    session.add(SavedSearch(name="Trucks", make="Toyota"))
    await session.commit()
    async with _client() as client:
        r = await client.get("/searches")
    assert r.status_code == 200
    assert "Trucks" in r.text


@pytest.mark.asyncio
async def test_detail_shows_matches_excluding_passed(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = Auction(
        source="t", source_auction_id="A", url="u", canonical_url="u",
        auction_subtype="estate", first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC), pickup_province="AB",
    )
    session.add(a)
    await session.flush()
    shown = AuctionLot(auction=a, source_lot_id="L1", url="u1", title="Shown Mustang",
                       make="Ford", model="Mustang", lot_status="open")
    passed = AuctionLot(auction=a, source_lot_id="L2", url="u2", title="Passed Mustang",
                        make="Ford", model="Mustang", lot_status="open")
    session.add_all([shown, passed])
    await session.flush()
    from carbuyer.db.enums import UserAction
    passed.user_action = UserAction.PASSED
    s = SavedSearch(name="stangs", make="Ford")
    session.add(s)
    await session.flush()
    session.add_all([
        SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=shown.id),
        SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=passed.id),
    ])
    await session.commit()

    async with _client() as client:
        r = await client.get(f"/searches/{s.id}")
    assert r.status_code == 200
    assert "Shown Mustang" in r.text
    assert "Passed Mustang" not in r.text


@pytest.mark.asyncio
async def test_detail_stamps_last_viewed_at(_patch_deps: AsyncSession) -> None:
    """Opening the detail view marks the search visited so the list's "N new"
    badge can reset (matches newer than last_viewed_at)."""
    session = _patch_deps
    s = SavedSearch(name="x", make="Ford")
    session.add(s)
    await session.commit()
    assert s.last_viewed_at is None
    async with _client() as client:
        r = await client.get(f"/searches/{s.id}")
    assert r.status_code == 200
    await session.refresh(s)
    assert s.last_viewed_at is not None


@pytest.mark.asyncio
async def test_dismiss_sets_dismissed_at(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    s = SavedSearch(name="x", make="Ford")
    session.add(s)
    await session.flush()
    m = SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=7)
    session.add(m)
    await session.commit()
    async with _client() as client:
        r = await client.post(f"/searches/{s.id}/dismiss/{m.id}")
    assert r.status_code in (200, 204)
    await session.refresh(m)
    assert m.dismissed_at is not None


@pytest.mark.asyncio
async def test_delete_cascades(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    s = SavedSearch(name="x")
    session.add(s)
    await session.flush()
    session.add(SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=1))
    await session.commit()
    sid = s.id
    async with _client() as client:
        r = await client.post(f"/searches/{sid}/delete")
    assert r.status_code in (200, 204, 303)
    assert (await session.get(SavedSearch, sid)) is None
    remaining = (await session.execute(
        select(SavedSearchMatch).where(SavedSearchMatch.saved_search_id == sid)
    )).scalars().all()
    assert remaining == []
```

(Note: HTML `DELETE` is awkward from forms, so the destructive route is
`POST /searches/{id}/delete`; the HTMX button uses `hx-post`. Keep it POST to
avoid a method-mismatch with the no-JS fallback.)

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/apps/dashboard/test_searches.py -q`
Expected: FAIL — the `/searches` routes 404 (router not registered).

- [ ] **Step 3: Implement the router**

Create `src/carbuyer/apps/dashboard/routers/searches.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import (
    CurrentUser,
    current_user,
    get_session,
)
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch
from carbuyer.db.notify import notify

router = APIRouter()

_SEARCH_CHANNEL = "saved_search_changed"


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


def _clean_int(value: str | None) -> int | None:
    v = _clean_str(value)
    return int(v) if v is not None else None


def _clean_list(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    cleaned = [v.strip() for v in values if v.strip()]
    return cleaned or None


def _apply_form(
    search: SavedSearch, *,
    name: str, make: str | None, model: str | None, trim: str | None,
    year_min: str | None, year_max: str | None, mileage_km_max: str | None,
    max_all_in_cost_cad: str | None,
    title_status: list[str] | None, condition_categorical: list[str] | None,
    province: list[str] | None, is_active: bool,
) -> None:
    search.name = name.strip() or "Untitled search"
    search.make = _clean_str(make)
    search.model = _clean_str(model)
    search.trim = _clean_str(trim)
    search.year_min = _clean_int(year_min)
    search.year_max = _clean_int(year_max)
    search.mileage_km_max = _clean_int(mileage_km_max)
    search.max_all_in_cost_cad = _clean_int(max_all_in_cost_cad)
    search.title_status = _clean_list(title_status)
    search.condition_categorical = _clean_list(condition_categorical)
    search.province = _clean_list(province)
    search.is_active = is_active


_MATCH_PAGE_SIZE = 20


async def _match_count(session: AsyncSession, search_id: int) -> int:
    rows = (await session.execute(
        select(SavedSearchMatch.id).where(
            SavedSearchMatch.saved_search_id == search_id,
            SavedSearchMatch.dismissed_at.is_(None),
        )
    )).scalars().all()
    return len(rows)


async def _new_count(session: AsyncSession, search: SavedSearch) -> int:
    """Live matches newer than the last detail-view visit (spec's 'N new')."""
    stmt = select(SavedSearchMatch.id).where(
        SavedSearchMatch.saved_search_id == search.id,
        SavedSearchMatch.dismissed_at.is_(None),
    )
    if search.last_viewed_at is not None:
        stmt = stmt.where(SavedSearchMatch.matched_at > search.last_viewed_at)
    return len((await session.execute(stmt)).scalars().all())


@router.get("/searches", response_class=HTMLResponse)
async def list_searches(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    searches = list((await session.execute(
        select(SavedSearch).order_by(SavedSearch.created_at.desc())
    )).scalars().all())
    counts = {s.id: await _match_count(session, s.id) for s in searches}
    new_counts = {s.id: await _new_count(session, s) for s in searches}
    return templates.TemplateResponse(
        request, "pages/searches_list.html",
        {
            "searches": searches, "counts": counts,
            "new_counts": new_counts, "active_subtab": "searches",
        },
    )


@router.get("/searches/new", response_class=HTMLResponse)
async def new_search(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/search_form.html", {"search": None},
    )


@router.post("/searches")
async def create_search(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    name: Annotated[str, Form()] = "",
    make: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    trim: Annotated[str | None, Form()] = None,
    year_min: Annotated[str | None, Form()] = None,
    year_max: Annotated[str | None, Form()] = None,
    mileage_km_max: Annotated[str | None, Form()] = None,
    max_all_in_cost_cad: Annotated[str | None, Form()] = None,
    title_status: Annotated[list[str] | None, Form()] = None,
    condition_categorical: Annotated[list[str] | None, Form()] = None,
    province: Annotated[list[str] | None, Form()] = None,
) -> Response:
    search = SavedSearch(name="x")
    _apply_form(
        search, name=name, make=make, model=model, trim=trim,
        year_min=year_min, year_max=year_max, mileage_km_max=mileage_km_max,
        max_all_in_cost_cad=max_all_in_cost_cad, title_status=title_status,
        condition_categorical=condition_categorical, province=province,
        is_active=True,
    )
    session.add(search)
    await session.flush()
    await notify(session, _SEARCH_CHANNEL, str(search.id))
    await session.commit()
    return RedirectResponse(f"/searches/{search.id}", status_code=303)


@router.get("/searches/{search_id}", response_class=HTMLResponse)
async def search_detail(
    request: Request,
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    page: int = 1,
) -> HTMLResponse:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return HTMLResponse("Not found", status_code=404)
    page = max(page, 1)

    # Current matches (paginated): join to the lot + auction, drop dismissed and
    # passed. Fetch one extra row to detect a next page without a COUNT.
    base = (
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
        .order_by(SavedSearchMatch.matched_at.desc())
    )
    rows = (await session.execute(
        base.offset((page - 1) * _MATCH_PAGE_SIZE).limit(_MATCH_PAGE_SIZE + 1)
    )).all()
    has_next = len(rows) > _MATCH_PAGE_SIZE
    matches = [
        {"lot": lot, "auction": auc, "match": m}
        for lot, auc, m in rows[:_MATCH_PAGE_SIZE]
    ]

    # Match-over-time activity log: every match incl. dismissed, newest first.
    log_rows = (await session.execute(
        select(SavedSearchMatch, AuctionLot.title)
        .join(AuctionLot, (SavedSearchMatch.source_kind == "auction_lot")
              & (SavedSearchMatch.source_id == AuctionLot.id))
        .where(SavedSearchMatch.saved_search_id == search_id)
        .order_by(SavedSearchMatch.matched_at.desc())
        .limit(50)
    )).all()
    activity = [{"match": m, "title": title} for m, title in log_rows]

    # Mark visited so the list's "N new" badge resets.
    search.last_viewed_at = datetime.now(UTC)
    await session.commit()

    return templates.TemplateResponse(
        request, "pages/search_detail.html",
        {
            "search": search, "matches": matches, "activity": activity,
            "page": page, "has_next": has_next, "active_subtab": "searches",
        },
    )


@router.get("/searches/{search_id}/edit", response_class=HTMLResponse)
async def edit_search(
    request: Request,
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request, "partials/search_form.html", {"search": search},
    )


@router.post("/searches/{search_id}/update")
async def update_search(
    request: Request,
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    name: Annotated[str, Form()] = "",
    make: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    trim: Annotated[str | None, Form()] = None,
    year_min: Annotated[str | None, Form()] = None,
    year_max: Annotated[str | None, Form()] = None,
    mileage_km_max: Annotated[str | None, Form()] = None,
    max_all_in_cost_cad: Annotated[str | None, Form()] = None,
    title_status: Annotated[list[str] | None, Form()] = None,
    condition_categorical: Annotated[list[str] | None, Form()] = None,
    province: Annotated[list[str] | None, Form()] = None,
    is_active: Annotated[str | None, Form()] = None,
) -> Response:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return HTMLResponse("Not found", status_code=404)
    _apply_form(
        search, name=name, make=make, model=model, trim=trim,
        year_min=year_min, year_max=year_max, mileage_km_max=mileage_km_max,
        max_all_in_cost_cad=max_all_in_cost_cad, title_status=title_status,
        condition_categorical=condition_categorical, province=province,
        is_active=(is_active is not None),
    )
    await notify(session, _SEARCH_CHANNEL, str(search.id))
    await session.commit()
    return RedirectResponse(f"/searches/{search.id}", status_code=303)


@router.post("/searches/{search_id}/dismiss/{match_id}")
async def dismiss_match(
    search_id: int,
    match_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    m = await session.get(SavedSearchMatch, match_id)
    if m is None or m.saved_search_id != search_id:
        return Response(status_code=404)
    if m.dismissed_at is None:
        m.dismissed_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=204)


@router.post("/searches/{search_id}/delete")
async def delete_search(
    search_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    search = await session.get(SavedSearch, search_id)
    if search is None:
        return Response(status_code=404)
    await session.delete(search)  # FK ondelete=CASCADE removes match rows
    await session.commit()
    return RedirectResponse("/searches", status_code=303)
```

- [ ] **Step 4: Register the router**

In `src/carbuyer/apps/dashboard/app.py`, add `searches` to the inner import
tuple and to the `include_router` loop:

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
        purchases,
        searches,
        sold,
        today,
        watched,
    )
    for router in (
        today, feed, closing, watched, searches, lots, comps, sold,
        purchases, health, actions, needs_plugin, auctions, admin,
    ):
        app.include_router(router.router)
```

- [ ] **Step 5: Create the templates**

`src/carbuyer/apps/dashboard/templates/pages/searches_list.html`:

```html
{% extends "base.html" %}
{% block nav %}watchlist{% endblock %}
{% block title %}Searches — CarBuyer{% endblock %}

{% block content %}
<section class="searches">
  {% include "partials/watchlist_subtabs.html" with context %}
  <div class="searches__head">
    <h1>Saved searches</h1>
    <a class="btn" hx-get="/searches/new" hx-target="#modal-slot" hx-swap="innerHTML"
       href="/searches/new">New search</a>
  </div>
  {% if not searches %}
    <p class="t-meta">No saved searches yet. Create one to get matched against new lots.</p>
  {% else %}
  <ul class="searches__list">
    {% for s in searches %}
      {% include "partials/search_card.html" with context %}
    {% endfor %}
  </ul>
  {% endif %}
</section>
{% endblock %}
```

`src/carbuyer/apps/dashboard/templates/partials/search_card.html`:

```html
{# Renders one saved-search summary. Expects `s`, `counts`, `new_counts`. #}
<li class="search-card">
  <a class="search-card__name" href="/searches/{{ s.id }}">{{ s.name }}</a>
  {% set new = new_counts.get(s.id, 0) %}
  {% if new %}<span class="search-card__new">{{ new }} new</span>{% endif %}
  <span class="search-card__badge">{{ counts.get(s.id, 0) }} matches</span>
  <span class="search-card__state {% if not s.is_active %}is-off{% endif %}">
    {{ "active" if s.is_active else "paused" }}
  </span>
</li>
```

`src/carbuyer/apps/dashboard/templates/pages/search_detail.html`:

```html
{% extends "base.html" %}
{% block nav %}watchlist{% endblock %}
{% block title %}{{ search.name }} — CarBuyer{% endblock %}

{% block content %}
<section class="search-detail">
  {% include "partials/watchlist_subtabs.html" with context %}
  <div class="search-detail__head">
    <h1>{{ search.name }}</h1>
    <div class="search-detail__actions">
      <a class="btn" hx-get="/searches/{{ search.id }}/edit"
         hx-target="#modal-slot" hx-swap="innerHTML"
         href="/searches/{{ search.id }}/edit">Edit</a>
      <form hx-post="/searches/{{ search.id }}/delete" hx-confirm="Delete this search?"
            method="post" action="/searches/{{ search.id }}/delete">
        <button type="submit" class="btn btn--danger">Delete</button>
      </form>
    </div>
  </div>

  <p class="t-meta">
    {{ search.make or "any make" }} ·
    {{ search.model or "any model" }} ·
    {% if search.year_min or search.year_max %}{{ search.year_min or "…" }}–{{ search.year_max or "…" }}{% else %}any year{% endif %}
    {% if search.province %} · {{ search.province | join(", ") }}{% endif %}
  </p>

  <h2>Current matches{% if page > 1 %} (page {{ page }}){% endif %}</h2>
  {% if not matches %}
    <p class="t-meta">No live matches{% if page > 1 %} on this page{% endif %}.</p>
  {% else %}
  <ul class="search-detail__matches">
    {% for row in matches %}
      <li class="match-row">
        <a href="/lots/{{ row.lot.id }}">{{ row.lot.title or ("Lot #" ~ row.lot.id) }}</a>
        <span class="t-meta">{{ row.auction.pickup_province or "" }}</span>
        <button class="btn btn--ghost"
                hx-post="/searches/{{ search.id }}/dismiss/{{ row.match.id }}"
                hx-target="closest .match-row" hx-swap="outerHTML">Dismiss</button>
      </li>
    {% endfor %}
  </ul>
  {% endif %}

  <nav class="pager" aria-label="Match pages">
    {% if page > 1 %}
      <a href="/searches/{{ search.id }}?page={{ page - 1 }}">← Newer</a>
    {% endif %}
    {% if has_next %}
      <a href="/searches/{{ search.id }}?page={{ page + 1 }}">Older →</a>
    {% endif %}
  </nav>

  <h2>Match activity</h2>
  {% if not activity %}
    <p class="t-meta">No matches recorded yet.</p>
  {% else %}
  <ol class="search-detail__activity">
    {% for row in activity %}
      <li class="activity-row {% if row.match.dismissed_at %}is-dismissed{% endif %}">
        <time datetime="{{ row.match.matched_at.isoformat() }}">
          {{ row.match.matched_at.strftime("%b %d, %H:%M") }}
        </time>
        <a href="/lots/{{ row.match.source_id }}">{{ row.title or ("Lot #" ~ row.match.source_id) }}</a>
        {% if row.match.dismissed_at %}<span class="t-meta">dismissed</span>{% endif %}
      </li>
    {% endfor %}
  </ol>
  {% endif %}
</section>
{% endblock %}
```

`src/carbuyer/apps/dashboard/templates/partials/search_form.html` (modal,
mirrors `bid_modal.html`; `search` is None for create, a row for edit):

```html
{# Saved-search create/edit modal. Swapped into #modal-slot. `search` is None
   for create or a SavedSearch for edit. #}
{% set is_edit = search is not none %}
{% set post_url = ("/searches/" ~ search.id ~ "/update") if is_edit else "/searches" %}
<div class="modal" role="dialog" aria-modal="true" aria-labelledby="search-modal-title">
  <a class="modal__backdrop" href="/modal/dismiss"
     hx-get="/modal/dismiss" hx-target="#modal-slot" hx-swap="innerHTML"
     aria-label="Close"></a>
  <div class="modal__panel">
    <h2 id="search-modal-title" class="modal__title">
      {{ "Edit search" if is_edit else "New search" }}
    </h2>
    <form class="modal__form search-form" method="post" action="{{ post_url }}"
          hx-post="{{ post_url }}" hx-target="body" hx-swap="outerHTML">
      <label class="modal__field">
        <span>Name</span>
        <input type="text" name="name" required
               value="{{ search.name if is_edit else '' }}" autofocus>
      </label>
      <div class="search-form__row">
        <label class="modal__field">
          <span>Make</span>
          <input type="text" name="make" value="{{ search.make or '' if is_edit else '' }}">
        </label>
        <label class="modal__field">
          <span>Model</span>
          <input type="text" name="model" value="{{ search.model or '' if is_edit else '' }}">
        </label>
      </div>
      <div class="search-form__row">
        <label class="modal__field">
          <span>Year min</span>
          <input type="number" name="year_min" value="{{ search.year_min or '' if is_edit else '' }}">
        </label>
        <label class="modal__field">
          <span>Year max</span>
          <input type="number" name="year_max" value="{{ search.year_max or '' if is_edit else '' }}">
        </label>
      </div>
      <label class="modal__field">
        <span>Provinces</span>
        <select name="province" multiple size="4">
          {% for p in ["AB", "SK", "MB", "BC", "ON"] %}
            <option value="{{ p }}"
              {% if is_edit and search.province and p in search.province %}selected{% endif %}>{{ p }}</option>
          {% endfor %}
        </select>
      </label>
      <details class="search-form__advanced">
        <summary>Advanced filters</summary>
        <label class="modal__field">
          <span>Trim contains</span>
          <input type="text" name="trim" value="{{ search.trim or '' if is_edit else '' }}">
        </label>
        <label class="modal__field">
          <span>Max mileage (km)</span>
          <input type="number" name="mileage_km_max"
                 value="{{ search.mileage_km_max or '' if is_edit else '' }}">
        </label>
        <label class="modal__field">
          <span>Max all-in cost (CAD)</span>
          <input type="number" name="max_all_in_cost_cad"
                 value="{{ search.max_all_in_cost_cad or '' if is_edit else '' }}">
        </label>
        <label class="modal__field">
          <span>Title status</span>
          <select name="title_status" multiple size="4">
            {% for t in ["NORMAL", "REBUILT", "SALVAGE", "UNKNOWN"] %}
              <option value="{{ t }}"
                {% if is_edit and search.title_status and t in search.title_status %}selected{% endif %}>{{ t }}</option>
            {% endfor %}
          </select>
        </label>
        <label class="modal__field">
          <span>Condition</span>
          <select name="condition_categorical" multiple size="4">
            {% for c in ["excellent", "good", "decent", "rough", "project"] %}
              <option value="{{ c }}"
                {% if is_edit and search.condition_categorical and c in search.condition_categorical %}selected{% endif %}>{{ c }}</option>
            {% endfor %}
          </select>
        </label>
        {% if is_edit %}
        <label class="modal__field modal__field--inline">
          <input type="checkbox" name="is_active" {% if search.is_active %}checked{% endif %}>
          <span>Active</span>
        </label>
        {% endif %}
      </details>
      <div class="modal__actions">
        <a class="modal__cancel" href="/modal/dismiss"
           hx-get="/modal/dismiss" hx-target="#modal-slot" hx-swap="innerHTML">Cancel</a>
        <button type="submit" class="modal__submit" hx-disabled-elt="this">
          {{ "Save" if is_edit else "Create" }}
        </button>
      </div>
    </form>
  </div>
</div>
```

`src/carbuyer/apps/dashboard/templates/partials/watchlist_subtabs.html`
(also used by Task 5):

```html
{# Lots/Searches sub-tab strip for the Watchlist section. `active_subtab`
   ("lots" | "searches") selects the current pill. #}
<nav class="subtabs" aria-label="Watchlist views">
  <a href="/watched" {% if active_subtab == 'lots' %}aria-current="page"{% endif %}>Lots</a>
  <a href="/searches" {% if active_subtab == 'searches' %}aria-current="page"{% endif %}>Searches</a>
</nav>
```

The `active_subtab` context value the sub-tab strip reads is already set by the
`list_searches` and `search_detail` handlers in Step 3, so no further handler
change is needed here. Both `searches_list.html` and `search_detail.html`
`{% include "partials/watchlist_subtabs.html" with context %}` at the top of
their `<section>` (shown in the templates above).

- [ ] **Step 6: Create the CSS and wire it in**

Create `src/carbuyer/apps/dashboard/static/css/components/searches.css`:

```css
.searches__head,
.search-detail__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--spacing-3);
  margin-block: var(--spacing-4);
}

.searches__list { list-style: none; padding: 0; margin: 0; }

.search-card {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
  padding: var(--spacing-3);
  border-bottom: 1px solid var(--color-rule);
}
.search-card__name { font-weight: 600; flex: 1; }
.search-card__badge {
  font-size: var(--text-sm);
  color: var(--color-accent-ink);
  background: var(--color-accent-bg);
  padding: 2px var(--spacing-2);
  border-radius: 999px;
}
.search-card__state { font-size: var(--text-sm); color: var(--color-muted); }
.search-card__state.is-off { color: var(--color-rule-strong); }

.subtabs {
  display: flex;
  gap: var(--spacing-2);
  border-bottom: 1px solid var(--color-rule);
  margin-bottom: var(--spacing-4);
}
.subtabs a {
  padding: var(--spacing-2) var(--spacing-3);
  color: var(--color-muted);
  text-decoration: none;
}
.subtabs a[aria-current="page"] {
  color: var(--color-ink);
  border-bottom: 2px solid var(--color-accent);
}

.search-form__row { display: flex; gap: var(--spacing-3); }
.search-form__advanced { margin-block: var(--spacing-3); }
.match-row,
.activity-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
  padding: var(--spacing-2) 0;
  border-bottom: 1px solid var(--color-rule);
}
.activity-row.is-dismissed { opacity: 0.55; }
.activity-row time { font-variant-numeric: tabular-nums; color: var(--color-muted); }

.search-card__new {
  font-size: var(--text-sm);
  color: var(--color-paper-3);
  background: var(--color-accent);
  padding: 2px var(--spacing-2);
  border-radius: 999px;
}

.pager { display: flex; gap: var(--spacing-4); margin-block: var(--spacing-3); }

/* Button family. The codebase styles modal buttons via .modal__submit /
   .modal__cancel; page-level controls here use a small shared .btn set. */
.btn {
  display: inline-flex;
  align-items: center;
  padding: var(--spacing-2) var(--spacing-3);
  border: 1px solid var(--color-rule-strong);
  border-radius: var(--spacing-1);
  background: var(--color-paper-3);
  color: var(--color-ink);
  text-decoration: none;
  cursor: pointer;
  font: inherit;
}
.btn--ghost { border-color: transparent; background: transparent; color: var(--color-muted); }
.btn--danger { border-color: var(--color-rule-strong); color: var(--color-accent-ink); }
```

In `src/carbuyer/apps/dashboard/static/css/tailwind.css`, add to the bottom
`@import` list (after `@import "./components/watchlist.css";`):

```css
@import "./components/searches.css";
```

- [ ] **Step 7: Run the dashboard tests**

Run: `uv run pytest tests/apps/dashboard/test_searches.py -q`
Expected: PASS.

- [ ] **Step 8: Rebuild CSS**

Run: `make css`
Expected: `app.css` regenerated with no errors.

- [ ] **Step 9: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/searches.py \
        src/carbuyer/apps/dashboard/templates/pages/searches_list.html \
        src/carbuyer/apps/dashboard/templates/pages/search_detail.html \
        src/carbuyer/apps/dashboard/templates/partials/search_form.html \
        src/carbuyer/apps/dashboard/templates/partials/search_card.html \
        src/carbuyer/apps/dashboard/templates/partials/watchlist_subtabs.html \
        src/carbuyer/apps/dashboard/static/css/components/searches.css \
        src/carbuyer/apps/dashboard/static/css/tailwind.css \
        src/carbuyer/apps/dashboard/static/css/app.css \
        src/carbuyer/apps/dashboard/app.py \
        tests/apps/dashboard/test_searches.py
git commit -m "feat(dashboard): saved-search CRUD + match view"
```

---

## Task 5: Watchlist sub-tab strip on `/watched`

**Files:**
- Modify: `src/carbuyer/apps/dashboard/templates/pages/watched.html`
- Modify: `src/carbuyer/apps/dashboard/routers/watched.py`
- Test: extend `tests/apps/dashboard/test_searches.py` (or the existing watched
  test file)

- [ ] **Step 1: Write the failing sub-tab test**

Add to `tests/apps/dashboard/test_searches.py`:

```python
@pytest.mark.asyncio
async def test_watched_shows_subtab_strip(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.get("/watched")
    assert r.status_code == 200
    assert 'href="/searches"' in r.text  # sub-tab links to Searches
    assert 'aria-label="Watchlist views"' in r.text


@pytest.mark.asyncio
async def test_searches_list_shows_subtab_strip(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.get("/searches")
    assert r.status_code == 200
    assert 'href="/watched"' in r.text
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/apps/dashboard/test_searches.py -k subtab -q`
Expected: FAIL — `/watched` has no `/searches` link yet.

- [ ] **Step 3: Render the sub-tab strip on `/watched`**

In `src/carbuyer/apps/dashboard/routers/watched.py`, add `active_subtab` to the
context:

```python
    return templates.TemplateResponse(
        request, "pages/watched.html",
        {"buckets": buckets, "active_subtab": "lots"},
    )
```

In `src/carbuyer/apps/dashboard/templates/pages/watched.html`, add the include
just inside the section (before the `<h1>`):

```html
{% block content %}
<section class="watched">
  {% include "partials/watchlist_subtabs.html" with context %}
  <h1>Watchlist</h1>
  {% include "partials/watchlist_board.html" %}
</section>
{% endblock %}
```

- [ ] **Step 4: Run the sub-tab tests**

Run: `uv run pytest tests/apps/dashboard/test_searches.py -k subtab -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/watched.py \
        src/carbuyer/apps/dashboard/templates/pages/watched.html \
        tests/apps/dashboard/test_searches.py
git commit -m "feat(dashboard): Lots/Searches sub-tabs on the Watchlist"
```

---

## Task 6: Lint, type-check, and final verification gate

**Files:** none (verification only; fix in place if anything fails).

- [ ] **Step 1: Ruff**

Run: `uv run ruff check src/carbuyer tests`
Expected: no NEW errors in the files this PR created/modified. For count/
threshold literals in tests that trip `PLR2004`, add `# noqa: PLR2004` with a
short reason (codebase convention; see existing tests). Hoist any inline
imports to module scope unless a circular import forces otherwise (the
dashboard `app.py` router import is the established exception and already
carries `# noqa: PLC0415`).

- [ ] **Step 2: Pyright (strict)**

Run: `uv run pyright src/carbuyer/db/saved_searches.py src/carbuyer/apps/search_matcher src/carbuyer/apps/dashboard/routers/searches.py`
Expected: no errors. The `worker.py` helpers are typed `session: AsyncSession`
(no `# type: ignore`); confirm no stray `session: object` slipped in, since
`object` has no `.execute` and would fail strict mode here.

- [ ] **Step 3: Full suite**

Run: `uv run pytest -q`
Expected: all green. New tests:
`tests/db/test_saved_search_models.py`, `tests/db/test_saved_search_matcher.py`,
`tests/apps/search_matcher/test_worker.py`, `tests/apps/dashboard/test_searches.py`.

- [ ] **Step 4: Manual smoke (optional but recommended)**

Run the dashboard (`uv run python -m carbuyer.apps.dashboard`), open `/searches`,
create a search via the modal, confirm it redirects to the detail page and a
`saved_search_changed` NOTIFY is emitted (visible if the matcher is running:
`uv run python -m carbuyer.apps.search_matcher`).

- [ ] **Step 5: Commit any lint/type fixes**

```bash
git add -A
git commit -m "chore(search): satisfy ruff/pyright for saved-search subsystem"
```

---

## Self-review (controller checklist — run after drafting)

- **Spec coverage:** §2.1 data model → Task 1 (both tables, all columns, unique
  + two indexes, cascade). §2.2 match semantics → Task 2 (`match_listing` AND /
  wildcard / case-insensitive / ANY-OF / range / inclusive caps; `passed`
  exclusion deferred to read paths per Decision 3). §2.3 worker → Task 3
  (LISTEN on the data-ready channels + `saved_search_changed`; idempotent
  insert; startup backfill as catchup). §2.4 dashboard CRUD → Task 4 (list with
  match-count + "N new since last visit" badges; new / create / detail with
  paginated current matches + match-over-time activity log; edit / update /
  dismiss / delete). The "N new" badge is backed by the `last_viewed_at` column
  (Task 1), stamped on each detail view. §2.5 navigation →
  Task 5 (Lots/Searches sub-tabs, topnav unchanged). §2.6 files → all created.
  §2.7 testing → pure matcher (Task 2), worker DB (Task 3), dashboard DB
  (Task 4/5). §2.8 risks → backfill bounded by `search_match_backfill_limit`.
- **Type consistency:** `MatchableListing` fields ↔ `adapt_auction_lot`
  outputs ↔ `match_listing` reads ↔ `SavedSearch` columns ↔ dashboard form
  field names (`make`, `model`, `trim`, `year_min`, `year_max`,
  `mileage_km_max`, `max_all_in_cost_cad`, `title_status`,
  `condition_categorical`, `province`, `is_active`) all agree. Match insert
  keys `(saved_search_id, source_kind, source_id)` match the unique index in
  both the model and the migration.
- **Placeholder scan:** no TBD/TODO; every code step is complete. The migration
  `down_revision` is the one value the implementer must fill from
  `alembic heads` (flagged explicitly in Task 1 Step 5).
- **Cross-task names:** worker channel constant `_SEARCH_CHANNEL =
  "saved_search_changed"` matches the dashboard router's `_SEARCH_CHANNEL`; both
  pass `^[a-z][a-z0-9_]{0,62}$`. The dashboard delete/dismiss routes are POST
  (not DELETE) and the templates post to them accordingly.
