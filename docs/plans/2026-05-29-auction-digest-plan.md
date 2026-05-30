# Per-Auction Digest Implementation Plan (PR-3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A cron-driven per-auction digest that batches each upcoming auction's
saved-search matches + rare/special vehicles into one Discord message ~24h
before the sale, plus a dashboard preview of that composition; and the
"two-stage rarity" tightening of `early_warning` so the long-lead alert and the
digest don't duplicate.

**Architecture:** A new `auction_digest` cron worker (systemd timer, every 15
min) selects auctions starting within 24h that haven't been digested, builds a
plaintext digest via a **pure** `compose_digest(...) -> str | None`, posts it
to Discord (`post_simple_message`) when non-empty, and stamps
`auctions.digest_sent_at`. A `GET /auctions/{id}/digest` dashboard route renders
the same composition as a live preview. `early_warning` is tightened to fire
only for top-rarity lots ≥7 days from close; the digest catches the rest at
T-24h. Composition queries data PR-1/PR-2 already produce.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (async), Alembic, FastAPI + Jinja2,
aiohttp (Discord REST), pytest/pytest-asyncio, ruff, pyright (strict). Tests run
against `carbuyer_test`; schema from `Base.metadata.create_all()`.

**Spec:** `docs/specs/2026-05-27-notification-pivot-design.md` (PR-3 section).

---

## Decisions that sharpen or deviate from the spec

1. **Plaintext digest, not a Discord embed.** The spec (§3.2) sketches a
   "DigestEmbed" and mentions Discord embed limits (25 fields, 1024 chars).
   But this codebase has **no embeds** — `discord_post.py` posts a plaintext
   `content` string, and the notifier builds messages via `render_*_text`
   functions (`notifier/messages.py`). The composer therefore returns a
   plaintext multi-line `str` (or `None` when empty) and the runner posts it
   with `post_simple_message(channel_id, content, session=...)` (no action-row
   buttons — appropriate for a digest). The real constraint is Discord's
   **2000-char message limit**, which the per-section cap-at-10 + truncation
   line keeps us well under.

2. **`early_warning` "≥7 days out" = 7 days to CLOSE.** The trigger only has
   `scheduled_end_at` (via `LotState`), and every existing time gate measures
   `scheduled_end_at - now`. So "the auction is ≥7 days out" is implemented as
   `(scheduled_end_at - now) >= timedelta(days=7)`. We do **not** add
   `scheduled_start_at` to `LotState` (minimal, consistent).
   **Add a new `long_lead_threshold` config field for the trigger; do NOT
   repurpose `early_warning_rarity_threshold`.** That existing field is also an
   input to the valuator's `_weights_hash` (`valuator.py:86`), which invalidates
   every cached score when any scoring tunable changes — so bumping it would
   trigger an unrelated mass re-valuation. Instead: add `long_lead_threshold`
   (~top-5%, e.g. 3.0) and have the notifier pass it as the early_warning rarity
   gate; leave `early_warning_rarity_threshold` at 2.0 untouched (it stays the
   valuator hash input). The time gate `early_warning_min_hours_to_close` IS
   bumped to 168 (7×24) — it is not in the valuator hash, so no re-valuation,
   but it asserts in `tests/shared/test_config.py`, which must be updated in the
   same commit. `evaluate_triggers`' logic is unchanged (only its comment, and
   the notifier's call now passes `long_lead_threshold`); the existing
   early_warning tests are re-pointed to the tightened values.

3. **Cron worker takes a singleton lock.** Like `source_watchdog` (the other
   cron worker that posts to Discord), `auction_digest` acquires
   `acquire_singleton_lock("auction_digest")` so two overlapping timer fires
   can't double-post. `main(now: datetime | None = None)` runs **once and
   returns** (no `listen()` loop). Order per spec §3.1: compose → post → stamp
   `digest_sent_at`. A crash between post and stamp re-posts next run (rare,
   at-least-once — acceptable, matches the notifier's semantics).

4. **passed/dismissed exclusion + cross-section dedup live in the RUNNER's
   queries; the composer is pure formatting.** So composer tests cover
   formatting / truncation / empty→None; the SQL-level `dismissed_at IS NULL`,
   `user_action != 'passed'`, and "section-2 excludes section-1 lot ids"
   filters are exercised by runner tests.

5. **No `base.html` nav change (deferred).** The spec §3.7 lists `base.html`
   ("auction page link target"), but the preview is an audit/verification tool
   reachable directly at `/auctions/{id}/digest`, and §3.5 frames it as
   groundwork for a *future* `/auctions/{id}` event view. Wiring a nav link
   belongs to that later view. The preview page sets `{% block nav %}auctions{% endblock %}`
   so the existing Auctions tab highlights. (If a link is wanted now, it's a
   one-line addition later — out of scope here.)

6. **systemd path is `infra/systemd/`** (spec §3.7 says `deploy/systemd/`; the
   real directory is `infra/systemd/`, as for every existing unit).

7. **Digest Discord channel:** the runner resolves `settings.discord_channels`
   at run start (via `channel_resolver.resolve_channels`, the notifier's
   pattern) and uses the `"auction_digest"` key, falling back to
   `"early_warning"` (the existing rare/lead-time alerts channel) when
   `"auction_digest"` is unconfigured — per spec §3.4 "defaulting to the
   existing alerts channel."

---

## Context the implementer must know

- **Cron worker shape** (mirror `src/carbuyer/apps/auction_distiller/distiller.py`):
  `async def main(now: datetime | None = None)` — if `now is None: now =
  datetime.now(UTC)`; collect eligible ids in one short read session; loop, each
  in its own `async with get_session() as s, s.begin():` with a per-row
  `try/except` that logs and continues; log a summary counter dict at the end.
  Entry `__main__.py` → `run_worker("auction_digest", main)` (from
  `carbuyer.apps._runner`). The optional `now` param is the test seam (no
  datetime monkeypatching).
- **Discord post:** `from carbuyer.apps.notifier.discord_post import
  post_simple_message`; `await post_simple_message(channel_id, content,
  session=http_session) -> bool`. Build one `aiohttp.ClientSession()` for the
  run. Channel resolution: `from carbuyer.apps.notifier.channel_resolver import
  resolve_channels`.
- **DB/session/log:** `get_session` from `carbuyer.db.session`; `get_logger`
  from `carbuyer.shared.logging`; `notify` is NOT needed (digest doesn't emit).
- **Models:** `Auction`, `AuctionLot`, `SavedSearch`, `SavedSearchMatch` in
  `carbuyer.db.models`. `AuctionLot` fields for rarity: `rarity_score`,
  `classic_or_collector`, `desirable_trim_or_spec`. `user_action` is a
  `SAEnum(native_enum=False)` String — compare to `UserAction.PASSED.value`.
- **Dashboard:** `auctions.py` router already exists (a stub with `GET
  /auctions`). Add the preview route to it. Use `templates` from
  `carbuyer.apps.dashboard.app`, `get_session` from
  `carbuyer.apps.dashboard.deps`. Tests: `AsyncClient(ASGITransport(app=app))`
  + monkeypatch `deps_mod.get_session_maker`.
- **Tests** build schema from `Base.metadata.create_all()` (the new
  `digest_sent_at` column appears automatically once added to the model). The
  `session` fixture rolls back per test; `func.now()` is the transaction
  timestamp (constant within a test) — set time-sensitive fields EXPLICITLY in
  tests, and use the worker's `now=` param for deterministic eligibility.

## File structure

```
Create:
  alembic/versions/<rev>_auction_digest.py
  src/carbuyer/apps/auction_digest/__init__.py            (empty)
  src/carbuyer/apps/auction_digest/__main__.py            (run_worker entry)
  src/carbuyer/apps/auction_digest/composer.py            (pure: data -> str | None)
  src/carbuyer/apps/auction_digest/runner.py              (query -> compose -> post -> stamp)
  src/carbuyer/apps/dashboard/templates/pages/auction_digest_preview.html
  infra/systemd/carbuyer-auction-digest.timer
  infra/systemd/carbuyer-auction-digest.service
  tests/apps/auction_digest/__init__.py                   (empty)
  tests/apps/auction_digest/test_composer.py              (pure)
  tests/apps/auction_digest/test_runner.py                (DB + mocked Discord)
  tests/apps/dashboard/test_auction_digest_preview.py     (DB)

Modify:
  src/carbuyer/db/models.py                 (+ Auction.digest_sent_at + partial index)
  src/carbuyer/shared/config.py             (+ long_lead_threshold, + digest_rarity_threshold; bump early_warning_min_hours_to_close to 168; leave early_warning_rarity_threshold)
  src/carbuyer/apps/notifier/triggers.py    (early_warning comment → long-lead intent; logic unchanged)
  src/carbuyer/apps/notifier/notifier.py    (pass rarity_threshold=settings.long_lead_threshold to evaluate_triggers)
  src/carbuyer/apps/notifier/channel_resolver.py  (doc the "auction_digest" key; no code change needed — keys are dynamic)
  src/carbuyer/apps/dashboard/routers/auctions.py (+ GET /auctions/{id}/digest)
  tests/apps/test_notifier_triggers.py      (re-point early_warning tests to tightened thresholds + regressions)
  tests/shared/test_config.py               (update early_warning_min_hours_to_close assertion 48 → 168)
```

`discord_post.py` is reused as-is. `channel_resolver.resolve_channels` already
handles arbitrary keys, so adding `"auction_digest"` is a config/env concern,
not a code change — Task 4 documents it.

---

## Task 1: Schema — `Auction.digest_sent_at` + eligibility index

**Files:** Modify `src/carbuyer/db/models.py`; Create
`alembic/versions/<rev>_auction_digest.py`; Test
`tests/db/test_auction_digest_models.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_auction_digest_models.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Auction


@pytest.mark.asyncio
async def test_auction_digest_sent_at_defaults_null(session: AsyncSession) -> None:
    a = Auction(
        source="t", source_auction_id="A1", url="u", canonical_url="u",
        auction_subtype="estate", first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()
    await session.refresh(a)
    assert a.digest_sent_at is None


@pytest.mark.asyncio
async def test_auction_digest_sent_at_settable(session: AsyncSession) -> None:
    a = Auction(
        source="t", source_auction_id="A2", url="u", canonical_url="u",
        auction_subtype="estate", first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        digest_sent_at=datetime(2026, 5, 29, tzinfo=UTC),
    )
    session.add(a)
    await session.flush()
    await session.refresh(a)
    assert a.digest_sent_at == datetime(2026, 5, 29, tzinfo=UTC)
```

- [ ] **Step 2: Run it — confirm fail**

Run: `uv run pytest tests/db/test_auction_digest_models.py -q`
Expected: FAIL — `AttributeError`/`TypeError` (no `digest_sent_at`).

- [ ] **Step 3: Add the column + index to the `Auction` model**

In `src/carbuyer/db/models.py`, in the `Auction` class, add (near the other
nullable timestamp columns, e.g. after `routing_resolved_at`):

```python
    # Owned by: auction_digest worker. NULL until the per-auction digest is
    # sent (or skipped-but-evaluated); the eligibility index filters on NULL.
    digest_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

And add a partial index to `Auction.__table_args__` (the spec's eligibility
index). The `Auction` class currently has `__table_args__ = (UniqueConstraint(...),)`;
extend it:

```python
    __table_args__ = (
        UniqueConstraint(
            "source", "source_auction_id",
            name="uq_auctions_source_source_auction_id",
        ),
        Index(
            "ix_auctions_digest_eligibility",
            "scheduled_start_at",
            postgresql_where=text("digest_sent_at IS NULL AND scheduled_start_at IS NOT NULL"),
        ),
    )
```

(`Index` and `text` are already imported in models.py.)

- [ ] **Step 4: Run it — confirm pass**

Run: `uv run pytest tests/db/test_auction_digest_models.py -q` → PASS.

- [ ] **Step 5: Hand-write the migration**

`uv run alembic revision -m "auction_digest"`, then set `down_revision` to the
current head (`uv run alembic heads`) and write:

```python
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column(
        "auctions",
        sa.Column("digest_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_auctions_digest_eligibility", "auctions", ["scheduled_start_at"],
        unique=False,
        postgresql_where=sa.text("digest_sent_at IS NULL AND scheduled_start_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_auctions_digest_eligibility", table_name="auctions")
    op.drop_column("auctions", "digest_sent_at")
```

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/db/models.py tests/db/test_auction_digest_models.py alembic/versions/
git commit -m "feat(db): add auctions.digest_sent_at + eligibility index"
```

---

## Task 2: Two-stage rarity — tighten `early_warning`, add digest threshold

**Files:** Modify `src/carbuyer/shared/config.py`,
`src/carbuyer/apps/notifier/triggers.py`,
`src/carbuyer/apps/notifier/notifier.py`; Modify
`tests/apps/test_notifier_triggers.py`, `tests/shared/test_config.py`.

The tightening is config + one notifier call-site change + a comment —
`evaluate_triggers`' logic is unchanged (it already gates on the passed
`rarity_threshold` and `early_warning_min_hours`). We add a NEW
`long_lead_threshold` and have the notifier pass it (rather than repurposing
`early_warning_rarity_threshold`, which the valuator's `_weights_hash` reads).
Two test files assert the old defaults and must be re-pointed:
`test_notifier_triggers.py` (the trigger thresholds) and `test_config.py` (the
`early_warning_min_hours_to_close` default).

- [ ] **Step 1: Update config**

In `src/carbuyer/shared/config.py`, change the early_warning defaults and add
the digest threshold (the block is currently
`early_warning_rarity_threshold: float = 2.0` /
`early_warning_min_hours_to_close: int = 48`):

```python
    # early_warning is the "long-lead / plan a road trip" signal (spec PR-3
    # §3.3): top-rarity lots, far enough out to act. Tightened so it doesn't
    # duplicate the T-24h digest.
    #
    # long_lead_threshold is NEW and is the early_warning rarity gate (~top 5%).
    # Do NOT reuse early_warning_rarity_threshold for this — that field is also
    # an input to the valuator's _weights_hash (valuator.py:86), so changing it
    # would invalidate every cached score and force a mass re-valuation. Leave it
    # at 2.0; the early_warning trigger reads long_lead_threshold instead (the
    # notifier passes it — Step 2b).
    early_warning_rarity_threshold: float = 2.0  # unchanged; valuator scoring-hash input
    long_lead_threshold: float = 3.0             # NEW: early_warning rarity gate
    early_warning_min_hours_to_close: int = 168  # was 48; 7 days (long-lead time gate)
    # The per-auction digest's rare/special section uses the lower bar (the
    # bulk of interesting cars), caught at T-24h.
    digest_rarity_threshold: float = 2.0
```

- [ ] **Step 2: Update the early_warning comment in triggers.py**

In `src/carbuyer/apps/notifier/triggers.py`, the early_warning block's logic is
unchanged; update only its leading comment so the intent is self-documenting:

```python
    # Early-warning (long-lead): a top-rarity car, not yet notified, far enough
    # from close to plan a trip. Tightened in PR-3 to the long-lead threshold +
    # >=7d-to-close (config) so it does not duplicate the T-24h auction digest.
    if (
        state.rarity_score is not None
        and state.rarity_score >= rarity_threshold
        and state.early_warning_notified_at is None
        and state.scheduled_end_at is not None
        and (state.scheduled_end_at - now) >= timedelta(hours=early_warning_min_hours)
    ):
        out.append(TriggerResult("early_warning", f"rarity={state.rarity_score}"))
```

(No `evaluate_triggers` signature/logic change. `early_warning_min_hours` is
still fed `settings.early_warning_min_hours_to_close` (now 168). The rarity gate
param `rarity_threshold` must now be fed the NEW `long_lead_threshold` — Step 2b.)

- [ ] **Step 2b: Point the notifier's early_warning rarity gate at `long_lead_threshold`**

In `src/carbuyer/apps/notifier/notifier.py`, the `evaluate_triggers(...)` call
currently passes `rarity_threshold=settings.early_warning_rarity_threshold`.
Change ONLY that kwarg to the new field (leave the other kwargs as-is):

```python
    triggers = evaluate_triggers(
        state,
        now=now,
        rarity_threshold=settings.long_lead_threshold,
        rescore_improvement_threshold=settings.rescore_improvement_threshold,
        early_warning_min_hours=settings.early_warning_min_hours_to_close,
    )
```

This is the only behavioral wiring change; `early_warning_rarity_threshold` is
now used solely by the valuator's `_weights_hash` (unchanged, so no
re-valuation).

- [ ] **Step 3: Re-point the early_warning tests + add regressions**

In `tests/apps/test_notifier_triggers.py`, update the shared constants to the
tightened values and ensure existing early_warning tests use a rarity that
clears the new bar and a `scheduled_end_at` ≥7d out. Change the constants
(currently `RARITY_THRESHOLD = 2.0`, `EARLY_WARNING_MIN_HOURS = 48`):

```python
RARITY_THRESHOLD = 3.0
RESCORE_IMPROVEMENT = 0.05
EARLY_WARNING_MIN_HOURS = 168
```

`_state`'s default `scheduled_end_at = NOW + timedelta(days=10)` already clears
7 days, so `test_early_warning_fires` (rarity 2.5) must bump its rarity above
3.0. Update it and `test_early_warning_ignores_showstopper` to `rarity_score=3.5`.
Then append regression tests:

```python
def test_early_warning_below_long_lead_threshold_does_not_fire() -> None:
    """A 2.5-rarity lot cleared the old 2.0 bar but not the long-lead 3.0."""
    s = _state(rarity_score=2.5)
    assert not any(t.trigger == "early_warning" for t in _run(s))


def test_early_warning_requires_seven_days_to_close() -> None:
    """Long-lead needs >=7d to close; 3d out does not fire even at high rarity."""
    near = _state(rarity_score=3.5, scheduled_end_at=NOW + timedelta(days=3))
    far = _state(rarity_score=3.5, scheduled_end_at=NOW + timedelta(days=7, hours=1))
    assert not any(t.trigger == "early_warning" for t in _run(near))
    assert any(t.trigger == "early_warning" for t in _run(far))
```

- [ ] **Step 3b: Update `tests/shared/test_config.py`**

That test asserts the old `early_warning_min_hours_to_close` default. It has
`DEFAULT_EARLY_WARNING_HOURS = 48` near the top and
`assert s.early_warning_min_hours_to_close == DEFAULT_EARLY_WARNING_HOURS`.
Change the constant to `168` (the bumped default). This must land in the same
commit as the config change or the full-suite gate (Task 6) fails.

- [ ] **Step 4: Run the affected tests + audit the shared-tunable consumers**

Run: `uv run pytest tests/apps/test_notifier_triggers.py tests/apps/test_notifier_worker.py tests/shared/test_config.py -q` → PASS.
Run: `grep -rn "early_warning_rarity_threshold\|early_warning_min_hours_to_close\|long_lead_threshold\|digest_rarity_threshold" src tests`. Expect exactly these consumers (confirm no others assert old defaults):
- `config.py` (definitions)
- `notifier/notifier.py` (passes `long_lead_threshold` + `early_warning_min_hours_to_close` into `evaluate_triggers`)
- `valuator/valuator.py:86` (`early_warning_rarity_threshold` in `_weights_hash` — **left unchanged on purpose**; do NOT touch it)
- `tests/apps/test_notifier_triggers.py` and `tests/shared/test_config.py` (updated above)

If the grep surfaces any other file asserting an old default, update it too.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/shared/config.py src/carbuyer/apps/notifier/triggers.py \
        src/carbuyer/apps/notifier/notifier.py \
        tests/apps/test_notifier_triggers.py tests/shared/test_config.py
git commit -m "feat(notifier): tighten early_warning to long-lead (top-rarity, >=7d); add digest thresholds"
```

---

## Task 3: Pure digest composer

**Files:** Create `src/carbuyer/apps/auction_digest/__init__.py` (empty),
`composer.py`; Test `tests/apps/auction_digest/__init__.py` (empty),
`test_composer.py`.

The composer is pure: it receives an already-filtered, already-deduped header +
two lot lists and formats the plaintext digest, capping each section at 10 and
returning `None` when both sections are empty.

- [ ] **Step 1: Write failing composer tests**

Create empty `tests/apps/auction_digest/__init__.py`, then
`tests/apps/auction_digest/test_composer.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from carbuyer.apps.auction_digest.composer import (
    DigestHeader,
    DigestLot,
    compose_digest,
)


def _header(**ov: object) -> DigestHeader:
    base: dict[str, object] = dict(
        auction_id=1, title="Graham Auctions", location="Headingley, MB",
        starts_at=datetime(2026, 3, 7, 16, 0, tzinfo=UTC),
        lot_count=47, vehicle_count=12, url="https://x/auction/1",
    )
    base.update(ov)
    return DigestHeader(**base)  # type: ignore[arg-type]


def _lot(i: int, *, search: str | None = None) -> DigestLot:
    return DigestLot(lot_id=i, summary=f"1968 Ford Mustang #{i}", search_name=search)


def test_empty_both_sections_returns_none() -> None:
    assert compose_digest(_header(), matches=[], rare=[]) is None


def test_only_matches() -> None:
    out = compose_digest(_header(), matches=[_lot(1, search="60s Mustang")], rare=[])
    assert out is not None
    assert "saved searches" in out.lower()
    assert "1968 Ford Mustang #1" in out
    assert "/lots/1" in out
    assert "rare" not in out.lower()  # no rare section when empty


def test_only_rare() -> None:
    out = compose_digest(_header(), matches=[], rare=[_lot(2)])
    assert out is not None
    assert "rare" in out.lower()
    assert "/lots/2" in out
    assert "saved searches" not in out.lower()


def test_both_sections_and_header() -> None:
    out = compose_digest(_header(), matches=[_lot(1, search="x")], rare=[_lot(2)])
    assert out is not None
    assert "Graham Auctions" in out
    assert "Headingley, MB" in out
    assert "/lots/1" in out and "/lots/2" in out


def test_truncates_each_section_at_ten() -> None:
    matches = [_lot(i) for i in range(1, 16)]  # 15
    out = compose_digest(_header(), matches=matches, rare=[])
    assert out is not None
    assert "/lots/10" in out
    assert "/lots/11" not in out      # capped at 10
    assert "5 more" in out            # "... and 5 more"


def test_respects_discord_2000_char_limit() -> None:
    matches = [_lot(i, search="search") for i in range(1, 11)]
    rare = [_lot(i) for i in range(11, 21)]
    out = compose_digest(_header(title="A" * 200), matches=matches, rare=rare)
    assert out is not None
    assert len(out) <= 2000
```

- [ ] **Step 2: Run — confirm fail**

Run: `uv run pytest tests/apps/auction_digest/test_composer.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the composer**

Create empty `src/carbuyer/apps/auction_digest/__init__.py`, then
`src/carbuyer/apps/auction_digest/composer.py`:

```python
"""Pure composition of a per-auction digest into a plaintext Discord message.

The runner supplies an already-filtered (dismissed/passed excluded, sections
deduped) header + two lot lists; this module only formats, caps each section at
SECTION_CAP, and returns None when there is nothing to send. Plaintext (not a
Discord embed) matches discord_post.post_simple_message; the result stays under
Discord's 2000-char message limit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SECTION_CAP = 10
_MAX_CONTENT = 2000


@dataclass(frozen=True, slots=True)
class DigestHeader:
    auction_id: int
    title: str
    location: str
    starts_at: datetime | None
    lot_count: int
    vehicle_count: int
    url: str


@dataclass(frozen=True, slots=True)
class DigestLot:
    lot_id: int
    summary: str
    search_name: str | None  # set for saved-search matches; None for rare


def _section(label: str, lots: list[DigestLot], bullet: str) -> list[str]:
    shown = lots[:SECTION_CAP]
    lines = [f"{label} ({len(lots)})"]
    for lot in shown:
        tag = f" [{lot.search_name}]" if lot.search_name else ""
        lines.append(f"  {bullet} {lot.summary}{tag} -> /lots/{lot.lot_id}")
    extra = len(lots) - len(shown)
    if extra > 0:
        lines.append(f"  ... and {extra} more -> {{auction_url}}")
    return lines


def compose_digest(
    header: DigestHeader,
    *,
    matches: list[DigestLot],
    rare: list[DigestLot],
) -> str | None:
    if not matches and not rare:
        return None

    when = header.starts_at.strftime("%a, %b %d - %H:%M UTC") if header.starts_at else "TBD"
    lines = [
        f"AUCTION: {header.title} - {header.location}",
        f"{when} | {header.lot_count} lots, {header.vehicle_count} vehicles | {header.url}",
        "",
    ]
    if matches:
        lines += _section("Your saved searches", matches, "*")
        lines.append("")
    if rare:
        lines += _section("Rare / special vehicles", rare, "-")
        lines.append("")
    lines.append("Cheap-deal alerts will arrive closer to close.")

    content = "\n".join(lines).replace("{auction_url}", header.url)
    if len(content) > _MAX_CONTENT:
        content = content[: _MAX_CONTENT - 1].rstrip() + "…"
    return content
```

- [ ] **Step 4: Run — confirm pass**

Run: `uv run pytest tests/apps/auction_digest/test_composer.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/auction_digest/__init__.py \
        src/carbuyer/apps/auction_digest/composer.py \
        tests/apps/auction_digest/__init__.py tests/apps/auction_digest/test_composer.py
git commit -m "feat(auction_digest): pure per-auction digest composer"
```

---

## Task 4: Cron runner + systemd unit

**Files:** Create `src/carbuyer/apps/auction_digest/__main__.py`, `runner.py`,
`infra/systemd/carbuyer-auction-digest.{timer,service}`; Test
`tests/apps/auction_digest/test_runner.py`.

- [ ] **Step 1: Write failing runner tests**

Create `tests/apps/auction_digest/test_runner.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.auction_digest import runner as runner_mod
from carbuyer.apps.auction_digest.runner import run_digests
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch

NOW = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)  # so "starts within 24h" = starts_at in (NOW, NOW+24h]


def _auction(session: AsyncSession, *, sid: str, starts_at: datetime | None,
             status: str = "upcoming", digest_sent_at: datetime | None = None) -> Auction:
    a = Auction(
        source="t", source_auction_id=sid, url=f"https://x/{sid}", canonical_url=f"https://x/{sid}",
        auction_subtype="estate", first_seen_at=NOW, last_seen_at=NOW,
        scheduled_start_at=starts_at, status=status, digest_sent_at=digest_sent_at,
        title=f"Auction {sid}", pickup_city="Headingley", pickup_province="MB",
    )
    session.add(a)
    return a


def _lot(session: AsyncSession, a: Auction, *, sid: str, **ov: object) -> AuctionLot:
    lot = AuctionLot(auction=a, source_lot_id=sid, url=f"https://x/{sid}",
                     title=f"Car {sid}", make="Ford", model="Mustang", year=1968,
                     lot_status="open", **ov)
    session.add(lot)
    return lot


@pytest.fixture
def _patched(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    maker = session.info["maker"]

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    monkeypatch.setattr(runner_mod, "get_session", fake_get_session)
    # Force a known channel id so no Discord resolution/network happens.
    monkeypatch.setattr(runner_mod, "_resolve_digest_channel", _fake_resolve)
    return session


async def _fake_resolve(_session: object) -> int:
    return 4242


@pytest.mark.asyncio
async def test_eligible_auction_with_matches_posts_and_marks(
    _patched: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched
    posts: list[tuple[int, str]] = []

    async def fake_post(channel_id: int, content: str, *, session: object = None) -> bool:
        posts.append((channel_id, content))
        return True

    monkeypatch.setattr(runner_mod, "post_simple_message", fake_post)

    a = _auction(session, sid="A1", starts_at=NOW + timedelta(hours=10))
    await session.flush()
    lot = _lot(session, a, sid="L1")
    s = SavedSearch(name="60s Mustang", make="Ford")
    session.add(s)
    await session.flush()
    session.add(SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=lot.id))
    await session.flush()

    await run_digests(now=NOW)

    assert len(posts) == 1
    assert posts[0][0] == 4242
    assert "Car L1" in posts[0][1]
    await session.refresh(a)
    assert a.digest_sent_at is not None


@pytest.mark.asyncio
async def test_empty_composition_marks_sent_without_posting(
    _patched: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched
    posts: list[object] = []

    async def fake_post(channel_id: int, content: str, *, session: object = None) -> bool:
        posts.append(content)
        return True

    monkeypatch.setattr(runner_mod, "post_simple_message", fake_post)
    a = _auction(session, sid="A1", starts_at=NOW + timedelta(hours=10))  # no lots/matches
    await session.flush()

    await run_digests(now=NOW)

    assert posts == []          # nothing to send
    await session.refresh(a)
    assert a.digest_sent_at is not None  # but marked, so it won't re-evaluate


@pytest.mark.asyncio
async def test_already_sent_and_out_of_window_skipped(
    _patched: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patched
    posts: list[object] = []

    async def fake_post(channel_id: int, content: str, *, session: object = None) -> bool:
        posts.append(content)
        return True

    monkeypatch.setattr(runner_mod, "post_simple_message", fake_post)
    _auction(session, sid="SENT", starts_at=NOW + timedelta(hours=5), digest_sent_at=NOW)
    _auction(session, sid="FAR", starts_at=NOW + timedelta(days=3))      # >24h out
    _auction(session, sid="PAST", starts_at=NOW - timedelta(hours=1))    # already started
    _auction(session, sid="CANCELLED", starts_at=NOW + timedelta(hours=5), status="cancelled")
    await session.flush()

    await run_digests(now=NOW)
    assert posts == []  # none eligible
```

- [ ] **Step 2: Run — confirm fail**

Run: `uv run pytest tests/apps/auction_digest/test_runner.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `runner.py`**

Create `src/carbuyer/apps/auction_digest/runner.py`:

```python
"""Per-auction digest cron runner: select eligible auctions, compose, post, mark.

Eligibility (spec PR-3 §3.1): scheduled_start_at set and within the next 24h,
not yet digested, status not cancelled/past. Each auction is processed in its
own short transaction; compose -> (if non-empty) post -> stamp digest_sent_at.
An empty composition still stamps digest_sent_at so it isn't re-evaluated for
24h. Single-instance (advisory lock in main) so overlapping timer fires can't
double-post."""
from __future__ import annotations

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func, text

from carbuyer.apps.auction_digest.composer import (
    DigestHeader,
    DigestLot,
    compose_digest,
)
from carbuyer.apps.notifier.channel_resolver import resolve_channels
from carbuyer.apps.notifier.discord_post import post_simple_message
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.shared.singleton import acquire_singleton_lock

log = get_logger("auction_digest")

_DIGEST_KEY = "auction_digest"
_FALLBACK_KEY = "early_warning"  # spec §3.4: default to the existing alerts channel
_SKIP_STATUSES = ("cancelled", "past")


def _lot_summary(lot: AuctionLot) -> str:
    parts = [str(lot.year or ""), lot.make or "", lot.model or "", lot.trim or ""]
    name = " ".join(p for p in parts if p) or (lot.title or f"Lot {lot.id}")
    if lot.mileage_km is not None:
        name += f" - {lot.mileage_km // 1000}k km"
    return name


async def _resolve_digest_channel(session: AsyncSession) -> int | None:
    """Resolve the digest channel id (auction_digest, falling back to the
    existing alerts channel). Returns None if neither is configured."""
    resolved = await resolve_channels(
        settings.discord_channels,
        guild_id=settings.discord_guild_id,
        bot_token=settings.discord_bot_token,
    )
    return resolved.get(_DIGEST_KEY) or resolved.get(_FALLBACK_KEY)


async def _eligible_auction_ids(session: AsyncSession, *, now: object) -> list[int]:
    stmt = (
        select(Auction.id)
        .where(
            Auction.scheduled_start_at.is_not(None),
            Auction.scheduled_start_at > now,
            Auction.scheduled_start_at - now <= text("interval '24 hours'"),
            Auction.digest_sent_at.is_(None),
            Auction.status.notin_(_SKIP_STATUSES),
        )
        .order_by(Auction.scheduled_start_at)
    )
    return list((await session.execute(stmt)).scalars().all())


def _passed_clause():  # noqa: ANN202
    return (AuctionLot.user_action.is_(None)) | (
        AuctionLot.user_action != UserAction.PASSED.value
    )


async def _build_sections(
    session: AsyncSession, auction: Auction,
) -> tuple[list[DigestLot], list[DigestLot]]:
    # Section 1: saved-search matches (dismissed + passed excluded), annotated.
    match_rows = (await session.execute(
        select(AuctionLot, SavedSearch.name)
        .join(SavedSearchMatch, (SavedSearchMatch.source_kind == "auction_lot")
              & (SavedSearchMatch.source_id == AuctionLot.id))
        .join(SavedSearch, SavedSearch.id == SavedSearchMatch.saved_search_id)
        .where(
            AuctionLot.auction_id == auction.id,
            SavedSearchMatch.dismissed_at.is_(None),
            _passed_clause(),
            AuctionLot.year.is_not(None),
        )
        .order_by(SavedSearchMatch.matched_at.desc(), SavedSearchMatch.id.desc())
    )).all()
    seen: set[int] = set()
    matches: list[DigestLot] = []
    for lot, search_name in match_rows:
        if lot.id in seen:
            continue
        seen.add(lot.id)
        matches.append(DigestLot(lot.id, _lot_summary(lot), search_name))

    # Section 2: rare/special, excluding lots already in section 1.
    rare_rows = (await session.execute(
        select(AuctionLot)
        .where(
            AuctionLot.auction_id == auction.id,
            AuctionLot.rarity_score.is_not(None),
            AuctionLot.rarity_score >= settings.digest_rarity_threshold,
            _passed_clause(),
            AuctionLot.year.is_not(None),
        )
        .order_by(AuctionLot.rarity_score.desc())
    )).scalars().all()
    rare = [DigestLot(lot.id, _lot_summary(lot), None) for lot in rare_rows if lot.id not in seen]
    return matches, rare


async def _vehicle_count(session: AsyncSession, auction_id: int) -> int:
    return (await session.execute(
        select(func.count()).select_from(AuctionLot).where(
            AuctionLot.auction_id == auction_id, AuctionLot.year.is_not(None),
        )
    )).scalar_one()


async def run_digests(*, now: object, http_session: aiohttp.ClientSession | None = None) -> dict[str, int]:
    """One cron pass. `now` is injected for deterministic tests."""
    async with get_session() as s:
        ids = await _eligible_auction_ids(s, now=now)
    counts = {"posted": 0, "empty": 0, "failed": 0}
    if not ids:
        log.info("auction_digest: no eligible auctions")
        return counts

    channel_id = await _resolve_digest_channel(_DummySession())  # resolve once
    if channel_id is None:
        log.error("auction_digest: no channel configured (auction_digest/early_warning)")
        return counts

    owns_session = http_session is None
    http = http_session or aiohttp.ClientSession()
    try:
        for auction_id in ids:
            try:
                async with get_session() as s, s.begin():
                    auction = await s.get(Auction, auction_id)
                    if auction is None or auction.digest_sent_at is not None:
                        continue
                    matches, rare = await _build_sections(s, auction)
                    total_lots = (await s.execute(
                        select(func.count()).select_from(AuctionLot).where(
                            AuctionLot.auction_id == auction_id,
                        )
                    )).scalar_one()
                    header = DigestHeader(
                        auction_id=auction.id,
                        title=auction.title or auction.source,
                        location=", ".join(
                            p for p in [auction.pickup_city, auction.pickup_province] if p
                        ) or "?",
                        starts_at=auction.scheduled_start_at,
                        lot_count=total_lots,
                        vehicle_count=await _vehicle_count(s, auction_id),
                        url=auction.canonical_url,
                    )
                    content = compose_digest(header, matches=matches, rare=rare)
                    if content is None:
                        auction.digest_sent_at = func.now()
                        counts["empty"] += 1
                        continue
                    ok = await post_simple_message(channel_id, content, session=http)
                    if ok:
                        auction.digest_sent_at = func.now()
                        counts["posted"] += 1
                    else:
                        counts["failed"] += 1
                        log.warning("auction_digest post failed", auction_id=auction_id)
            except Exception:
                log.exception("auction_digest: auction failed", auction_id=auction_id)
                counts["failed"] += 1
    finally:
        if owns_session:
            await http.close()
    log.info("auction_digest complete", **counts)
    return counts


class _DummySession:
    """resolve_channels doesn't touch the DB; this satisfies the signature
    without opening a session just for channel resolution."""


async def main(now: object | None = None) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to keep the test seam clean

    lock_conn = await acquire_singleton_lock("auction_digest")
    try:
        if now is None:
            now = datetime.now(UTC)
        async with aiohttp.ClientSession() as http:
            await run_digests(now=now, http_session=http)
    finally:
        await lock_conn.close()
```

> Implementer note: `_resolve_digest_channel` takes a session param only so the
> test can monkeypatch it with a no-arg-friendly stub; `resolve_channels` itself
> needs no DB. If pyright objects to `_DummySession`, simplify
> `_resolve_digest_channel` to take no args and have the test patch it directly
> (the test already patches `runner_mod._resolve_digest_channel`). Keep whichever
> is pyright-clean; the behavior (resolve once per run, fall back to
> `early_warning`) is what matters.

- [ ] **Step 4: Implement `__main__.py`**

```python
from carbuyer.apps._runner import run_worker
from carbuyer.apps.auction_digest.runner import main

if __name__ == "__main__":
    run_worker("auction_digest", main)
```

- [ ] **Step 5: Run runner tests — confirm pass**

Run: `uv run pytest tests/apps/auction_digest/test_runner.py -q` → PASS.
(If the `now`/interval comparison needs a Python timedelta instead of SQL
`interval`, express the window in Python: compute `window_end = now +
timedelta(hours=24)` and filter `Auction.scheduled_start_at <= window_end` — do
whichever keeps the test green and pyright clean.)

- [ ] **Step 6: systemd timer + service**

Create `infra/systemd/carbuyer-auction-digest.service` (copy
`carbuyer-distiller.service`, change Description + ExecStart to `-m
carbuyer.apps.auction_digest`; `Type=oneshot` if the distiller uses it, else
`simple`). Create `infra/systemd/carbuyer-auction-digest.timer` (copy
`carbuyer-distiller.timer`) with `OnUnitActiveSec=15min`, `OnBootSec=5min`,
`Persistent=true`, `Unit=carbuyer-auction-digest.service`, `[Install]
WantedBy=timers.target`. Mirror the exact field set the distiller uses.

- [ ] **Step 7: Commit**

```bash
git add src/carbuyer/apps/auction_digest/runner.py \
        src/carbuyer/apps/auction_digest/__main__.py \
        tests/apps/auction_digest/test_runner.py \
        infra/systemd/carbuyer-auction-digest.timer \
        infra/systemd/carbuyer-auction-digest.service
git commit -m "feat(auction_digest): cron runner (query -> compose -> post -> mark) + systemd timer"
```

---

## Task 5: Dashboard digest preview route

**Files:** Modify `src/carbuyer/apps/dashboard/routers/auctions.py`; Create
`templates/pages/auction_digest_preview.html`; Test
`tests/apps/dashboard/test_auction_digest_preview.py`.

`GET /auctions/{id}/digest` renders the SAME composition (reusing the runner's
section-builder) as a read-only preview — no posting, no `digest_sent_at` write.

- [ ] **Step 1: Write failing preview test**

Create `tests/apps/dashboard/test_auction_digest_preview.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from carbuyer.apps.dashboard import deps as deps_mod
from carbuyer.apps.dashboard.app import app
from carbuyer.db.models import Auction, AuctionLot, SavedSearch, SavedSearchMatch


@pytest.fixture
def _patch_deps(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    maker: async_sessionmaker[AsyncSession] = session.info["maker"]
    monkeypatch.setattr(deps_mod, "get_session_maker", lambda: maker)
    return session


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_preview_renders_matches_and_rare(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = Auction(source="t", source_auction_id="A", url="u", canonical_url="u",
                auction_subtype="estate", first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC), title="Graham Auctions",
                pickup_province="MB", scheduled_start_at=datetime.now(UTC) + timedelta(hours=10))
    session.add(a)
    await session.flush()
    matched = AuctionLot(auction=a, source_lot_id="L1", url="u1", title="Matched Mustang",
                         make="Ford", model="Mustang", year=1968, lot_status="open")
    rarelot = AuctionLot(auction=a, source_lot_id="L2", url="u2", title="Rare Viper",
                         make="Dodge", model="Viper", year=2005, lot_status="open", rarity_score=4.0)
    session.add_all([matched, rarelot])
    await session.flush()
    s = SavedSearch(name="60s Mustang", make="Ford")
    session.add(s)
    await session.flush()
    session.add(SavedSearchMatch(saved_search_id=s.id, source_kind="auction_lot", source_id=matched.id))
    await session.commit()

    async with _client() as client:
        r = await client.get(f"/auctions/{a.id}/digest")
    assert r.status_code == 200
    assert "Matched Mustang" in r.text or "Ford Mustang" in r.text
    assert "Rare Viper" in r.text or "Dodge Viper" in r.text


@pytest.mark.asyncio
async def test_preview_empty_state(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    a = Auction(source="t", source_auction_id="A", url="u", canonical_url="u",
                auction_subtype="estate", first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC), title="Empty Auction", pickup_province="MB")
    session.add(a)
    await session.commit()
    async with _client() as client:
        r = await client.get(f"/auctions/{a.id}/digest")
    assert r.status_code == 200
    assert "nothing" in r.text.lower() or "no " in r.text.lower()


@pytest.mark.asyncio
async def test_preview_404_unknown_auction(_patch_deps: AsyncSession) -> None:
    async with _client() as client:
        r = await client.get("/auctions/999999/digest")
    assert r.status_code == 404
```

- [ ] **Step 2: Run — confirm fail**

Run: `uv run pytest tests/apps/dashboard/test_auction_digest_preview.py -q`
Expected: FAIL — route 404s for the valid auction (not implemented).

- [ ] **Step 3: Add the preview route**

In `src/carbuyer/apps/dashboard/routers/auctions.py`, add imports and the route
(reuse the runner's section builder to guarantee preview == sent):

```python
from typing import Annotated

from fastapi import Depends
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.auction_digest.runner import _build_sections  # pyright: ignore[reportPrivateUsage]
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import Auction


@router.get("/auctions/{auction_id}/digest", response_class=HTMLResponse)
async def auction_digest_preview(
    request: Request,
    auction_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    auction = await session.get(Auction, auction_id)
    if auction is None:
        return Response("Not found", status_code=404)
    matches, rare = await _build_sections(session, auction)
    return templates.TemplateResponse(
        request, "pages/auction_digest_preview.html",
        {"auction": auction, "matches": matches, "rare": rare},
    )
```

> If importing `_build_sections` from the worker reads as too tight a coupling,
> the reviewer may prefer lifting `_build_sections` + the `DigestLot`/`_lot_summary`
> helpers into `composer.py` (pure-ish, no posting) and importing from there in
> both runner and router. Either is acceptable; preview MUST use the same
> section logic as the runner so the preview matches what gets sent.

- [ ] **Step 4: Create the template**

`src/carbuyer/apps/dashboard/templates/pages/auction_digest_preview.html`:

```html
{% extends "base.html" %}
{% block nav %}auctions{% endblock %}
{% block title %}{{ auction.title or auction.source }} — digest preview{% endblock %}

{% block content %}
<section class="digest-preview">
  <h1>{{ auction.title or auction.source }}</h1>
  <p class="t-meta">
    {{ auction.pickup_city or "" }}{% if auction.pickup_province %}, {{ auction.pickup_province }}{% endif %}
    {% if auction.scheduled_start_at %} · starts {{ auction.scheduled_start_at.strftime("%a %b %d, %H:%M UTC") }}{% endif %}
    {% if auction.digest_sent_at %} · digest sent {{ auction.digest_sent_at.strftime("%b %d %H:%M") }}{% endif %}
  </p>

  {% if not matches and not rare %}
    <p class="t-meta">Nothing to digest — no saved-search matches or rare vehicles on this auction.</p>
  {% endif %}

  {% if matches %}
  <h2>Your saved searches ({{ matches | length }})</h2>
  <ul>
    {% for lot in matches %}
      <li><a href="/lots/{{ lot.lot_id }}">{{ lot.summary }}</a>
        {% if lot.search_name %}<span class="t-meta">[{{ lot.search_name }}]</span>{% endif %}</li>
    {% endfor %}
  </ul>
  {% endif %}

  {% if rare %}
  <h2>Rare / special vehicles ({{ rare | length }})</h2>
  <ul>
    {% for lot in rare %}
      <li><a href="/lots/{{ lot.lot_id }}">{{ lot.summary }}</a></li>
    {% endfor %}
  </ul>
  {% endif %}
</section>
{% endblock %}
```

- [ ] **Step 5: Run preview tests — confirm pass**

Run: `uv run pytest tests/apps/dashboard/test_auction_digest_preview.py -q` → PASS.
Confirm the app still imports: `uv run python -c "from carbuyer.apps.dashboard.app import app"`.

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/auctions.py \
        src/carbuyer/apps/dashboard/templates/pages/auction_digest_preview.html \
        tests/apps/dashboard/test_auction_digest_preview.py
git commit -m "feat(dashboard): auction digest preview page"
```

---

## Task 6: Lint, type-check, full-suite gate

**Files:** none (verification; fix in place).

- [ ] **Step 1: Ruff** — `uv run ruff check src/carbuyer/apps/auction_digest src/carbuyer/apps/dashboard/routers/auctions.py src/carbuyer/apps/notifier/triggers.py src/carbuyer/shared/config.py src/carbuyer/db/models.py tests/apps/auction_digest tests/apps/dashboard/test_auction_digest_preview.py tests/apps/test_notifier_triggers.py tests/db/test_auction_digest_models.py` → no new errors. Add `# noqa: PLR2004` for count/threshold literals in tests per codebase convention; hoist inline imports unless a circular import forces otherwise.
- [ ] **Step 2: Pyright** — `uv run pyright src/carbuyer/apps/auction_digest src/carbuyer/apps/dashboard/routers/auctions.py src/carbuyer/apps/notifier/triggers.py` → 0 errors. Resolve any `_DummySession`/`now: object` typing by tightening to `datetime`/`AsyncSession` as noted in Task 4.
- [ ] **Step 3: Full suite** — `uv run pytest -q` → all green.
- [ ] **Step 4: Channel-config doc** — add `"auction_digest"` to the `DISCORD_CHANNELS` example in the repo's env/README/`.env.example` if one exists (so ops knows the new key; the resolver handles it dynamically — no code change). If no such file, skip.
- [ ] **Step 5: Commit any fixes** — `git commit -m "chore(auction_digest): satisfy ruff/pyright"`.

---

## Self-review (controller checklist)

- **Spec coverage:** §3.1 cron + eligibility query → Task 4. §3.2 composition
  (3 sections, dedup, cap-10, skip-empty-still-mark) → Task 3 (compose) + Task 4
  (queries/dedup/mark). §3.3 two-stage rarity → Task 2. §3.4 channel key →
  Task 4 (`_resolve_digest_channel`, fallback). §3.5 preview → Task 5. §3.6 data
  model → Task 1. §3.7 files → all (systemd under `infra/`, base.html deferred
  per Decision 5). §3.8 testing → composer pure (Task 3), runner DB+mock
  (Task 4), preview DB (Task 5). §3.9 risks → documented; eligibility index
  backs the hot query.
- **Type/name consistency:** `DigestHeader`/`DigestLot` fields ↔ composer ↔
  runner's `_build_sections` output ↔ preview template (`lot.lot_id`,
  `lot.summary`, `lot.search_name`). `digest_sent_at` column ↔ migration ↔
  eligibility query ↔ index WHERE clause all agree.
- **Deviations** (Decisions 1–7) are each justified and reversible; the biggest
  (plaintext not embed) is forced by the actual `discord_post` API.
- **Placeholder scan:** none; `down_revision` is the one value to fill from
  `alembic heads` (flagged in Task 1).
- **Cross-task risk:** Task 5 imports `_build_sections` from Task 4's runner so
  preview == sent; the plan flags lifting it to a shared module if a reviewer
  prefers looser coupling.
