# Going-Cheap Tiering Implementation Plan (PR-1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the notifier's single going-cheap threshold with a
time-to-close tier table (0.15 @ T-1h, 0.30 @ T-6h, 0.50 @ T-24h; no alert
beyond T-24h), unified across watched and unwatched lots.

**Architecture:** A pure `cheap_threshold(time_to_close)` function returns the
firing threshold for the closest (tightest) tier whose window contains the
time-to-close, or `None` if too far out or already closed. `evaluate_triggers`
calls it in place of the old `notify_threshold` comparison. No schema change,
no new files.

**Tech Stack:** Python 3.14, pytest, pytest-asyncio, ruff, pyright. The
notifier is `src/carbuyer/apps/notifier/`.

**Spec:** `docs/specs/2026-05-27-notification-pivot-design.md` (PR-1 section).

---

## Context the implementer must know

- **`evaluate_triggers` is pure** (`triggers.py`) — takes a `LotState` snapshot
  + threshold params, returns `list[TriggerResult]`. It is called from exactly
  one place: `notifier.py:190`. There is one test file for it:
  `tests/apps/test_notifier_triggers.py`.
- **`notifier.py` worker** loads a lot, builds `LotState`, calls
  `evaluate_triggers`, then posts. Its tests
  (`tests/apps/test_notifier_worker.py`) run against the **real wall-clock**
  `datetime.now(UTC)` and a test DB, seeding lots via a `_seed_lot` helper whose
  default close date is `datetime(2026, 6, 10)` — ~14 days past "now"
  (2026-05-27). **Under tiering, that default is beyond T-24h, so going-cheap
  stops firing for every worker test that relied on it.** This is the bulk of
  the work.
- **Two behaviors that the OLD code allowed and the NEW code forbids:**
  1. Watched lots (`interested`/`bid_placed`) used to fire going-cheap *at any
     time-to-close*. They now honor the tier window like everything else.
  2. `early_warning` (needs ≥48h to close) and `going_cheap` (needs ≤24h) can
     **no longer co-occur on one lot** — disjoint windows. The only pair that
     can still co-fire is `going_cheap` + `closing_soon` (both at T-1h).
- **Quiet-hours deferral of `going_cheap` is now structurally unreachable**
  (a sub-0.30 going-cheap can only happen in the T-1h tier, which always
  triggers the closing-in-1h quiet-hours override). Per decision: remove the
  one test that exercised this, keep the quiet-hours code.
- **`settings.notify_threshold` has a second consumer** (`valuator.py:85`).
  Do **not** delete it. PR-1 only stops the notifier's going-cheap path from
  reading it.
- **`last_cheap_score` is always `None` in production** (`notifier.py:59` —
  no DB column yet), so the rescore-improvement re-fire path is only exercised
  by trigger unit tests that pass it explicitly. Leave that as-is.

## File structure

```
Modify:
  src/carbuyer/apps/notifier/triggers.py
    - add GOING_CHEAP_TIERS constant + cheap_threshold() pure function
    - rewire the going_cheap block in evaluate_triggers
    - signature: drop `notify_threshold`, add `going_cheap_tiers` (defaulted)
  src/carbuyer/apps/notifier/notifier.py
    - line ~190: drop the `notify_threshold=settings.notify_threshold` kwarg
  tests/apps/test_notifier_triggers.py
    - _run helper: drop notify_threshold; remove NOTIFY_THRESHOLD constant
    - migrate 5 existing going_cheap tests to tier semantics
    - add cheap_threshold unit tests + tier-integration tests
  tests/apps/test_notifier_worker.py
    - move 8 going_cheap tests into a firing tier window (T-6h: now+3h, 0.30)
    - rework partial-success test to going_cheap + closing_soon co-fire
    - remove the now-impossible quiet-hours deferral test
```

No new files. No migration. No `settings` change.

---

## Task 1: `cheap_threshold` pure function + unit tests

**Files:**
- Modify: `src/carbuyer/apps/notifier/triggers.py`
- Test: `tests/apps/test_notifier_triggers.py`

This task is purely additive — `evaluate_triggers` is untouched, so the full
suite stays green. TDD applies cleanly.

- [ ] **Step 1: Write the failing unit test**

Add to the **bottom** of `tests/apps/test_notifier_triggers.py`:

```python
# ─── PR-1: going-cheap tier table ──────────────────────────────────────────


def test_cheap_threshold_tier_boundaries() -> None:
    from carbuyer.apps.notifier.triggers import cheap_threshold

    # T-1h tier (<= 1h): 0.15
    assert cheap_threshold(timedelta(minutes=30)) == 0.15
    assert cheap_threshold(timedelta(hours=1)) == 0.15
    # just past T-1h falls to the T-6h tier: 0.30
    assert cheap_threshold(timedelta(hours=1, minutes=1)) == 0.30
    assert cheap_threshold(timedelta(hours=6)) == 0.30
    # just past T-6h falls to the T-24h tier: 0.50
    assert cheap_threshold(timedelta(hours=6, minutes=1)) == 0.50
    assert cheap_threshold(timedelta(hours=24)) == 0.50
    # beyond the widest tier: no alert
    assert cheap_threshold(timedelta(hours=24, minutes=1)) is None
    assert cheap_threshold(timedelta(days=3)) is None
    # already closed: no alert
    assert cheap_threshold(timedelta(minutes=-5)) is None
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/apps/test_notifier_triggers.py::test_cheap_threshold_tier_boundaries -v`
Expected: FAIL — `ImportError: cannot import name 'cheap_threshold'`.

- [ ] **Step 3: Implement the constant + function**

In `src/carbuyer/apps/notifier/triggers.py`, after the `TriggerResult`
dataclass (around line 35, before `_CLOSING_SOON_WINDOW`), add:

```python
# Going-cheap thresholds as a function of time-to-close. Tightest window
# first: a lot 30 min from close uses the T-1h threshold, not T-24h. Beyond
# the widest tier going-cheap never fires — a nominal opening bid days out is
# not signal. See docs/specs/2026-05-27-notification-pivot-design.md (PR-1).
GOING_CHEAP_TIERS: tuple[tuple[timedelta, float], ...] = (
    (timedelta(hours=1), 0.15),
    (timedelta(hours=6), 0.30),
    (timedelta(hours=24), 0.50),
)


def cheap_threshold(
    time_to_close: timedelta,
    tiers: tuple[tuple[timedelta, float], ...] = GOING_CHEAP_TIERS,
) -> float | None:
    """Minimum price_deal_score for a going-cheap alert at this time-to-close.

    Returns the threshold of the closest (tightest) tier whose window contains
    time_to_close, or None when the lot is already closed or further out than
    the widest tier.
    """
    if time_to_close < timedelta(0):
        return None
    for window, threshold in tiers:
        if time_to_close <= window:
            return threshold
    return None
```

- [ ] **Step 4: Run it to confirm it passes**

Run: `uv run pytest tests/apps/test_notifier_triggers.py::test_cheap_threshold_tier_boundaries -v`
Expected: PASS.

- [ ] **Step 5: Confirm nothing else broke**

Run: `uv run pytest tests/apps/test_notifier_triggers.py tests/apps/test_notifier_worker.py -q`
Expected: all green (the new function is not yet wired in).

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/apps/notifier/triggers.py tests/apps/test_notifier_triggers.py
git commit -m "feat(notifier): add going-cheap time-to-close tier table"
```

---

## Task 2: Rewire `evaluate_triggers` + migrate trigger/worker tests

**Files:**
- Modify: `src/carbuyer/apps/notifier/triggers.py`
- Modify: `src/carbuyer/apps/notifier/notifier.py`
- Modify: `tests/apps/test_notifier_triggers.py`
- Modify: `tests/apps/test_notifier_worker.py`

This is one atomic commit by necessity: changing the going-cheap contract
breaks both test files simultaneously, so they must land together to keep the
commit green. Implementation and test migration are done together; the
full-suite run at the end is the verification gate.

### 2a — Change the implementation

- [ ] **Step 1: Update the `evaluate_triggers` signature**

In `triggers.py`, the current signature (lines ~47-55):

```python
def evaluate_triggers(
    state: LotState,
    *,
    now: datetime,
    rarity_threshold: float,
    notify_threshold: float,
    rescore_improvement_threshold: float,
    early_warning_min_hours: int,
) -> list[TriggerResult]:
```

becomes (drop `notify_threshold`, add `going_cheap_tiers`):

```python
def evaluate_triggers(
    state: LotState,
    *,
    now: datetime,
    rarity_threshold: float,
    rescore_improvement_threshold: float,
    early_warning_min_hours: int,
    going_cheap_tiers: tuple[tuple[timedelta, float], ...] = GOING_CHEAP_TIERS,
) -> list[TriggerResult]:
```

- [ ] **Step 2: Replace the going-cheap block**

The current block (lines ~77-101):

```python
    if (
        not state.has_showstopper
        and state.confidence_bucket in {"medium", "high"}
        and (state.flag_score or 0) >= -1
        and state.price_deal_score is not None
        and state.price_deal_score >= notify_threshold
    ):
        closing_in_24h = (
            state.scheduled_end_at is not None
            and state.scheduled_end_at - now <= timedelta(hours=24)
        )
        eligible_user = state.user_action in {"interested", "bid_placed", None}
        fires_for_watched = state.user_action in {"interested", "bid_placed"}
        fires_for_unflagged = closing_in_24h

        should_fire = False
        if state.cheap_notified_at is None and (fires_for_watched or fires_for_unflagged):
            should_fire = True
        elif state.last_cheap_score is not None and (
            state.price_deal_score - state.last_cheap_score
        ) >= rescore_improvement_threshold:
            should_fire = True

        if should_fire and eligible_user:
            out.append(TriggerResult("going_cheap", f"score={state.price_deal_score}"))
```

becomes:

```python
    # Going-cheap: the alert bar drops as close approaches (see cheap_threshold).
    # All quality gates are block-scoped so later triggers (closing_soon,
    # lot_extended) aren't short-circuited. user_action is already known to be
    # one of {interested, bid_placed, None} here — passed/purchased returned
    # above — so no per-user gate is needed.
    if (
        not state.has_showstopper
        and state.confidence_bucket in {"medium", "high"}
        and (state.flag_score or 0) >= -1
        and state.price_deal_score is not None
        and state.scheduled_end_at is not None
    ):
        threshold = cheap_threshold(state.scheduled_end_at - now, going_cheap_tiers)
        if threshold is not None and state.price_deal_score >= threshold:
            should_fire = state.cheap_notified_at is None or (
                state.last_cheap_score is not None
                and (state.price_deal_score - state.last_cheap_score)
                >= rescore_improvement_threshold
            )
            if should_fire:
                out.append(
                    TriggerResult(
                        "going_cheap",
                        f"score={state.price_deal_score} threshold={threshold}",
                    )
                )
```

- [ ] **Step 3: Update the only production caller**

In `notifier.py` (lines ~190-197), remove the `notify_threshold` kwarg:

```python
    triggers = evaluate_triggers(
        state,
        now=now,
        rarity_threshold=settings.early_warning_rarity_threshold,
        rescore_improvement_threshold=settings.rescore_improvement_threshold,
        early_warning_min_hours=settings.early_warning_min_hours_to_close,
    )
```

### 2b — Migrate `tests/apps/test_notifier_triggers.py`

- [ ] **Step 4: Update the `_run` helper and remove the dead constant**

Delete the line `NOTIFY_THRESHOLD = 0.15` (near line 9). Change `_run`
(lines ~51-59) to drop the `notify_threshold` kwarg:

```python
def _run(state: LotState) -> list[TriggerResult]:
    return evaluate_triggers(
        state,
        now=NOW,
        rarity_threshold=RARITY_THRESHOLD,
        rescore_improvement_threshold=RESCORE_IMPROVEMENT,
        early_warning_min_hours=EARLY_WARNING_MIN_HOURS,
    )
```

- [ ] **Step 5: Replace `test_going_cheap_fires_for_watched_anytime`**

Replace the whole function (lines ~68-75) with:

```python
def test_going_cheap_watched_honors_tier_window() -> None:
    """Watched lots no longer fire going_cheap days out — they honor the same
    time-to-close tiers as everything else."""
    far = _state(
        price_deal_score=0.20,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(days=10),
    )
    near = _state(
        price_deal_score=0.20,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(far))
    assert any(t.trigger == "going_cheap" for t in _run(near))
```

- [ ] **Step 6: Replace `test_going_cheap_for_unflagged_only_when_closing_soon`**

Replace the whole function (lines ~78-84) with:

```python
def test_going_cheap_unflagged_honors_tier_window() -> None:
    """Unflagged lots fire only inside a tier window when the deal clears that
    tier's threshold. 0.20 clears the T-1h tier (0.15) but not anything wider."""
    far = _state(price_deal_score=0.20, scheduled_end_at=NOW + timedelta(days=10))
    near = _state(price_deal_score=0.20, scheduled_end_at=NOW + timedelta(minutes=30))
    assert not any(t.trigger == "going_cheap" for t in _run(far))
    assert any(t.trigger == "going_cheap" for t in _run(near))
```

- [ ] **Step 7: Split `test_showstopper_suppresses_going_cheap_but_allows_early_warning`**

The old test asserted both behaviors on one 10-day-out lot. early_warning needs
≥48h-to-close; an in-window going_cheap test needs ≤24h — disjoint. Replace the
single function (lines ~93-103) with two:

```python
def test_showstopper_suppresses_going_cheap() -> None:
    """In-window lot with a great deal but a showstopper flag: no going_cheap."""
    s = _state(
        price_deal_score=0.60,
        has_showstopper=True,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))


def test_early_warning_ignores_showstopper() -> None:
    """Showstopper gates going_cheap, not early_warning — a rare car closing
    far out still earns its lead-time alert."""
    s = _state(
        rarity_score=2.5,
        has_showstopper=True,
        scheduled_end_at=NOW + timedelta(days=10),
    )
    assert any(t.trigger == "early_warning" for t in _run(s))
```

- [ ] **Step 8: Move the flag-score and confidence tests in-window**

Replace `test_bad_flag_score_suppresses_going_cheap` (lines ~106-113) with:

```python
def test_bad_flag_score_suppresses_going_cheap() -> None:
    s = _state(
        price_deal_score=0.60,
        flag_score=-2,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))
```

Replace `test_low_confidence_suppresses_going_cheap` (lines ~116-123) with:

```python
def test_low_confidence_suppresses_going_cheap() -> None:
    s = _state(
        price_deal_score=0.60,
        confidence_bucket="low",
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))
```

- [ ] **Step 9: Move the purchased-suppression test in-window**

Replace `test_going_cheap_suppressed_when_purchased` (lines ~231-239) with:

```python
def test_going_cheap_suppressed_when_purchased() -> None:
    """A lot the user already bought generates no going_cheap pings, even with
    a screaming deal inside a firing window."""
    s = _state(
        price_deal_score=0.60,
        user_action="purchased",
        scheduled_end_at=NOW + timedelta(minutes=30),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))
```

- [ ] **Step 10: Add tier-integration tests**

Append to the bottom of `tests/apps/test_notifier_triggers.py` (after the
`test_cheap_threshold_tier_boundaries` added in Task 1):

```python
def test_going_cheap_t24h_needs_screaming_deal() -> None:
    """~20h out (T-24h tier): only a >= 0.50 deal fires."""
    end = NOW + timedelta(hours=20)
    weak = _state(price_deal_score=0.40, user_action="interested", scheduled_end_at=end)
    strong = _state(price_deal_score=0.55, user_action="interested", scheduled_end_at=end)
    assert not any(t.trigger == "going_cheap" for t in _run(weak))
    assert any(t.trigger == "going_cheap" for t in _run(strong))


def test_going_cheap_t6h_needs_solid_deal() -> None:
    """~3h out (T-6h tier): 0.35 fires, 0.20 does not."""
    end = NOW + timedelta(hours=3)
    weak = _state(price_deal_score=0.20, user_action="interested", scheduled_end_at=end)
    ok = _state(price_deal_score=0.35, user_action="interested", scheduled_end_at=end)
    assert not any(t.trigger == "going_cheap" for t in _run(weak))
    assert any(t.trigger == "going_cheap" for t in _run(ok))


def test_going_cheap_too_far_out_never_fires() -> None:
    """Even a perfect deal does not fire beyond the widest tier."""
    s = _state(
        price_deal_score=0.99,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(days=3),
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))


def test_going_cheap_rescore_improvement_refires() -> None:
    """An already-notified lot re-fires only when the deal improved past the
    rescore threshold."""
    s = _state(
        price_deal_score=0.30,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
        cheap_notified_at=NOW - timedelta(hours=2),
        last_cheap_score=0.20,  # delta 0.10 >= RESCORE_IMPROVEMENT (0.05)
    )
    assert any(t.trigger == "going_cheap" for t in _run(s))


def test_going_cheap_no_refire_without_improvement() -> None:
    s = _state(
        price_deal_score=0.30,
        user_action="interested",
        scheduled_end_at=NOW + timedelta(minutes=30),
        cheap_notified_at=NOW - timedelta(hours=2),
        last_cheap_score=0.29,  # delta 0.01 < RESCORE_IMPROVEMENT (0.05)
    )
    assert not any(t.trigger == "going_cheap" for t in _run(s))
```

- [ ] **Step 11: Run the trigger tests**

Run: `uv run pytest tests/apps/test_notifier_triggers.py -v`
Expected: all PASS.

### 2c — Migrate `tests/apps/test_notifier_worker.py`

The fix pattern for going-cheap worker tests: set `scheduled_end_at` to
`datetime.now(UTC) + timedelta(hours=3)` (T-6h tier, threshold 0.30). This
fires going-cheap for a 0.30 deal **without** also firing `closing_soon`
(which needs ≤1h). Do **not** use `now + 30min` for these — that is the T-1h
tier and would add a `closing_soon` trigger, breaking the "exactly 1 post"
assertions.

- [ ] **Step 12: `test_process_one_fires_going_cheap` — set in-window end**

In the `_seed_lot(...)` call (lines ~130-137), add a close date. The call
becomes:

```python
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
        notification_status="in_progress",
    )
```

- [ ] **Step 13: Apply the same `scheduled_end_at` to the other going-cheap tests**

Add `scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),` to the
`_seed_lot(...)` calls in each of these tests (they currently rely on the
default 2026-06-10 date):

- `test_process_one_post_failure_keeps_pending_and_increments_attempts` (~176)
- `test_process_one_post_failure_flips_failed_at_max_attempts` (~218)
- `test_process_one_unconfigured_channel_marks_skipped` (~258)
- `test_process_one_already_notified_cheap_skips` (~514)
- `test_process_pending_self_notifies_on_transient_failure` (~733)

For `test_process_pending_claims_and_processes` (~404), the lots are built
inline (not via `_seed_lot`) and share one `Auction`. Change that auction's
`scheduled_end_at` (line ~427) from `datetime(2026, 6, 10, tzinfo=UTC)` to:

```python
        scheduled_end_at=datetime.now(UTC) + timedelta(hours=3),
```

- [ ] **Step 14: Rework `test_process_one_partial_success_marks_done`**

early_warning + going_cheap can no longer co-occur. Use going_cheap +
closing_soon (both fire at T-1h for a watched, actively-closing lot). Replace
the seed + setup portion (lines ~303-316) so the function reads:

```python
async def test_process_one_partial_success_marks_done(
    _patched_get_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two triggers, first succeeds, second fails. Outcome is DONE — at least
    one message landed. going_cheap + closing_soon are the only pair that can
    co-fire on one lot after tiering (both at T-1h)."""
    session = _patched_get_session
    soon_end = datetime.now(UTC) + timedelta(minutes=30)
    _, lot = _seed_lot(
        session,
        price_deal_score=0.30,  # going_cheap fires at the T-1h tier (>= 0.15)
        confidence_bucket="high",
        flag_score=0,
        user_action="interested",
        scheduled_end_at=soon_end,
        notification_status="in_progress",
    )
    lot.lot_status = "closing_soon"  # closing_soon trigger also fires
    await session.flush()

    call_count = {"n": 0}

    async def fake_post(
        channel_id: int, content: str, lot_id: int, *, session: object = None,
    ) -> bool:
        call_count["n"] += 1
        return call_count["n"] == 1  # first succeeds, rest fail

    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr(
        "carbuyer.apps.notifier.notifier.settings.discord_channels",
        {"hot_deals": 2, "watchlist": 3, "auction_closing": 4},
    )

    http = MagicMock()
    outcome = await _process_one(lot.id, http_session=http)
    assert outcome == "done"

    await session.refresh(lot)
    assert lot.notification_status == NotificationStatus.DONE
```

- [ ] **Step 15: Fix `test_process_one_quiet_hours_override_fires_high_score_going_cheap`**

This test (lines ~664-702) uses `far_end = datetime(2099, 1, 1)` with score
0.40. Under tiering a 2099 lot never fires going-cheap. The test's point is the
*score* override (0.40 ≥ 0.30 fires through quiet hours), so put the lot in the
T-6h tier where 0.40 clears the 0.30 threshold but is not within 1h (so the
override under test is the score, not closing-in-1h). Change `far_end`
(line ~673) to:

```python
    near_end = datetime.now(UTC) + timedelta(hours=3)
```

and update the `scheduled_end_at=far_end` reference (line ~680) to
`scheduled_end_at=near_end`. (Rename the local for clarity; leave score 0.40.)

- [ ] **Step 16: Remove the now-impossible quiet-hours deferral test**

Delete `test_process_one_defers_during_quiet_hours_for_low_score_going_cheap`
entirely (lines ~614-660). A sub-0.30 going-cheap can only occur in the T-1h
tier, which always triggers the closing-in-1h quiet-hours override — so the
deferral scenario is structurally unreachable after tiering. (The score-override
path is still covered by Step 15; the closing-in-1h override by
`test_process_one_quiet_hours_override_fires_closing_in_1h`.)

- [ ] **Step 17: Run the full notifier suite**

Run: `uv run pytest tests/apps/test_notifier_worker.py tests/apps/test_notifier_triggers.py -v`
Expected: all PASS. (`test_process_one_quiet_hours_override_fires_closing_in_1h`
at line ~805 already uses `now + 30min` and needs **no** change — confirm it
still passes.)

- [ ] **Step 18: Run the entire test suite**

Run: `uv run pytest -q`
Expected: all PASS. Nothing outside the notifier should be affected.

- [ ] **Step 19: Commit**

```bash
git add src/carbuyer/apps/notifier/triggers.py \
        src/carbuyer/apps/notifier/notifier.py \
        tests/apps/test_notifier_triggers.py \
        tests/apps/test_notifier_worker.py
git commit -m "feat(notifier): tier going-cheap alerts by time-to-close

Replace the single notify_threshold with a 0.15/0.30/0.50 tier table at
T-1h/T-6h/T-24h; no alert beyond T-24h. Unifies watched and unwatched lots.
Migrates trigger and worker tests to the tier windows and removes the
quiet-hours deferral test made unreachable by tiering."
```

---

## Task 3: Lint, type-check, and final verification gate

**Files:** none (verification only; fix in place if anything fails).

- [ ] **Step 1: Ruff**

Run: `uv run ruff check src/carbuyer/apps/notifier tests/apps/test_notifier_triggers.py tests/apps/test_notifier_worker.py`
Expected: no errors. If a magic-number `PLR2004` fires on a bare numeric
assertion, add `# noqa: PLR2004` (codebase convention for HTTP/threshold
literals in tests) or bind the literal to a named local.

- [ ] **Step 2: Pyright**

Run: `uv run pyright src/carbuyer/apps/notifier`
Expected: no new errors. The `going_cheap_tiers` parameter type is
`tuple[tuple[timedelta, float], ...]`; ensure the constant and the parameter
default agree.

- [ ] **Step 3: Full suite once more**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 4: Commit any lint/type fixes (only if Steps 1-2 required changes)**

```bash
git add -A
git commit -m "chore(notifier): satisfy ruff/pyright for going-cheap tiering"
```

---

## Self-review (controller checklist — already run)

- **Spec coverage:** PR-1 section of the spec maps to Tasks 1-2. The tier
  numbers (0.15/0.30/0.50 @ T-1h/T-6h/T-24h), the "no alert beyond T-24h" rule,
  the watched/unwatched unification, and the dedup/rescore preservation are all
  implemented. ✓
- **Placeholder scan:** no TBD/TODO; every code step shows complete code.
  `<rev>`-style placeholders are absent (no migration in this PR). ✓
- **Type consistency:** `cheap_threshold` signature, `GOING_CHEAP_TIERS` type,
  and the `going_cheap_tiers` parameter all use
  `tuple[tuple[timedelta, float], ...]`. `evaluate_triggers` no longer takes
  `notify_threshold`; the sole caller (`notifier.py`) and the test `_run`
  helper are both updated to match. ✓
- **Line numbers are approximate** (`~NNN`) because earlier edits shift them;
  every step also quotes the exact text to find, so they remain unambiguous.
