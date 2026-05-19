# Four-State `user_action` Migration — Design

Spec for the dashboard-redesign foundation work: replace the existing 3-value `user_action` enum with a 4-state workflow and add the bid/win tracking needed by the rest of the redesign.

Decisions feeding this spec are in the memory file `dashboard-redesign-direction-a` (locked 2026-05-19). This document was rewritten on 2026-05-19 after multi-agent review surfaced a class of "historical preservation" footguns; the audit-table approach below replaces the original in-row preservation design.

## Goals

- Replace `UserAction` (`interested` / `maybe` / `not_interested`) with the 4-state workflow (`interested` / `bid_placed` / `purchased` / `passed`).
- Add columns that the workflow requires for **current state**: `max_bid_cad`, `bid_placed_at`, `won_at` on `AuctionLot`.
- Track **transition history** in a new `lot_action_history` table — every state change writes a row with timestamp, new state, optional bid amount, and source (`dashboard` / `discord_bot` / `migration`).
- Remove the transitional alias hack in `apps/dashboard/routers/actions.py` (lines 26–45).
- Collapse `AuctionLot.was_purchased_by_us` into `user_action='purchased'` — one fact, one column.
- Keep persistent Discord buttons in posted notification history working — no `AttributeError` and no silent enum-downgrade of `purchased`/`bid_placed` lots.
- Tighten downstream behavior that the new states implicitly broke: notifier short-circuit on purchased lots, derived-interest set excludes purchased.

## Non-goals (deliberate, follow-up PRs)

- Watchlist kanban UI polish on `/watched`. This PR ships a 4-bucket flat list; kanban is a follow-up.
- Place-bid modal UX. The data model + CHECK constraints + `actions.py` form-field plumbing land here; the modal lands next.
- Auctions / Garage / Admin section content — per memory, expansion.
- A separate `lot_state.py` for an upcoming "auto-detect bid_placed from bidder identity" feature — see memory `[[auctioneer-bid-number-auto-detect]]`.

## Design decisions

| Question | Answer |
|---|---|
| Backfill | Lossy. `maybe → interested`, `not_interested → passed`, `AuctionLot.was_purchased_by_us=true → purchased`. Migration order: enum remap first, then `purchased` overwrite — purchased always wins. |
| Scope | Full migration in one alembic revision: enum + new columns + new history table + CHECK constraints + drop `AuctionLot.was_purchased_by_us`. |
| `AuctionLot.was_purchased_by_us` | Dropped. `user_action='purchased'` is the source of truth. |
| `HistoricalSale.was_purchased_by_us` | **Untouched.** Different semantics: marks comp-pricing rows that originated from our own wins, used to filter/weight comp data. Independent fact, stays as-is. |
| Discord `LotMaybeButton` on legacy messages | Class stays registered; callback writes `INTERESTED` via `apply_user_action(..., allow_downgrade=False)`. |
| Discord `LotNotInterestedButton` on legacy messages | Class stays registered; callback writes `PASSED` via `apply_user_action(..., allow_downgrade=False)`. |
| New notification button row | Two buttons: Interested, Pass. |
| Historical state semantics | Current state on `AuctionLot`; transition history in `lot_action_history`. Bid/win columns clear on exit from their states — bidirectional CHECK enforces this. |
| `apply_user_action` placement | `carbuyer/db/lot_state.py`. Same package as `models.py`/`enums.py`. Avoids cross-app dependency from `apps/bot/` → `apps/dashboard/`. |
| `UserAction` column type | Tightened to `Mapped[UserAction | None]` (still `String(16)` underneath). Mypy now catches typos in string comparisons. |

## Schema

### `db/enums.py` — UserAction rewrite

```python
class UserAction(StrEnum):
    INTERESTED = "interested"
    BID_PLACED = "bid_placed"
    PURCHASED = "purchased"
    PASSED = "passed"
```

DB column stays `String(16)`. Longest value (`bid_placed`) is 10 chars.

### `db/models.py` — `AuctionLot` changes

**Tighten existing column type:**
```python
user_action: Mapped[UserAction | None] = mapped_column(
    SAEnum(UserAction, native_enum=False, length=16),
    index=True,
)
```
`native_enum=False` keeps the underlying column as `VARCHAR(16)` — no Postgres enum type creation, matches the current shape, no schema-only-rename pain on future enum changes.

**Add (all nullable — populated only while in the bound state):**
```python
max_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
bid_placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

**Drop:** `was_purchased_by_us` on `AuctionLot` only. The same-named column on `HistoricalSale` (`models.py:483`) is intentionally untouched — it's a comp-data tag, distinct fact.

**Bidirectional CHECK constraints** (current-state columns *must* be set when in-state and *must* be null otherwise):
```python
CheckConstraint(
    "(user_action = 'bid_placed') = (max_bid_cad IS NOT NULL)",
    name="ck_auction_lots_bid_placed_iff_max_bid",
)
CheckConstraint(
    "(user_action = 'bid_placed') = (bid_placed_at IS NOT NULL)",
    name="ck_auction_lots_bid_placed_iff_timestamp",
)
CheckConstraint(
    "(user_action = 'purchased') = (won_at IS NOT NULL)",
    name="ck_auction_lots_purchased_iff_won_at",
)
```
The bidirectional form (`A = B`) means both conditions must agree. This prevents stale data after state changes — `apply_user_action` is forced to clear fields when transitioning out, or the DB rejects the write.

**ORM helpers** (defense against the "read column without checking state" footgun):
```python
@hybrid_property
def is_active_bid(self) -> bool:
    return self.user_action == UserAction.BID_PLACED

@hybrid_property
def is_purchased(self) -> bool:
    return self.user_action == UserAction.PURCHASED
```
Hybrid properties keep these usable in queries too (`select(AuctionLot).where(AuctionLot.is_active_bid)`). Keep additions minimal — only the two booleans, no `current_bid_amount` etc. (callers can just read `max_bid_cad` directly; the CHECK guarantees it's only set when `bid_placed`).

### `db/models.py` — new `LotActionHistory` model

```python
class LotActionHistory(Base):  # NOT TimestampMixin — single immutable timestamp
    __tablename__ = "lot_action_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("auction_lots.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The user_action being entered. NULL = explicit clear (toggle-off).
    user_action: Mapped[UserAction | None] = mapped_column(
        SAEnum(UserAction, native_enum=False, length=16),
    )
    # Bid amount AT the time of transition. NULL when transition isn't into bid_placed.
    # Preserves the "what max did I commit to" answer even after the lot moves out
    # of bid_placed and AuctionLot.max_bid_cad has been cleared.
    max_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # When the transition happened. Distinct from TimestampMixin.created_at to make
    # the field's semantic explicit at the schema layer.
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    # Where the write came from. Free-form short string. Today: "dashboard",
    # "discord_bot", "migration". Future writers extend the vocabulary.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
```

**Why not** `TimestampMixin`: `created_at` and `changed_at` would be redundant; rows are immutable so no `updated_at`. One explicit column is honest.

**No relationship back-pop on `AuctionLot`** — history rows would inflate the AuctionLot object graph and most reads don't need them. Callers that need history do an explicit query.

**Index:** `ix_lot_action_history_lot_id_changed_at` on `(lot_id, changed_at DESC)` for the "show last N transitions for this lot" query the audit UI will eventually need. The single `lot_id` index from `index=True` above is dropped in favor of this composite (covered).

### Alembic migration — single revision, single transaction

```
1. CREATE TABLE lot_action_history (...)
2. ADD COLUMN auction_lots.max_bid_cad     NUMERIC(12, 2)
3. ADD COLUMN auction_lots.bid_placed_at   TIMESTAMPTZ
4. ADD COLUMN auction_lots.won_at          TIMESTAMPTZ
5. UPDATE auction_lots SET user_action = 'interested' WHERE user_action = 'maybe'
6. UPDATE auction_lots SET user_action = 'passed'     WHERE user_action = 'not_interested'
7. UPDATE auction_lots SET user_action = 'purchased',
                           won_at      = COALESCE(updated_at, now())
   WHERE was_purchased_by_us = TRUE      -- runs LAST: purchased wins over passed
8. INSERT INTO lot_action_history (lot_id, user_action, max_bid_cad, changed_at, source)
   SELECT id, user_action, NULL, COALESCE(updated_at, now()), 'migration'
   FROM auction_lots WHERE user_action IS NOT NULL
9. ALTER TABLE auction_lots DROP COLUMN was_purchased_by_us
10. ALTER TABLE auction_lots ADD CONSTRAINT ck_..._iff_max_bid  CHECK (...)
11. ALTER TABLE auction_lots ADD CONSTRAINT ck_..._iff_timestamp CHECK (...)
12. ALTER TABLE auction_lots ADD CONSTRAINT ck_..._iff_won_at   CHECK (...)
```

**Ordering invariant — explicit:** Step 7 must run *after* step 6. A row with both `was_purchased_by_us=TRUE` AND `user_action='not_interested'` (legitimate scenario: user vetoed a lot, admin/import script later marked it bought from a partner event) lands at `purchased`, not `passed`. The migration docstring states this; the test asserts it (see Testing).

**Step 8 seeds history** so admin queries don't see a state-machine that "started today". A single synthetic row per pre-existing labeled lot is enough; we don't reconstruct prior maybe→interested transitions.

**Downgrade** is symmetric and lossy: drop CHECKs, re-add `was_purchased_by_us` (`server_default=false`), backfill `TRUE` where `user_action='purchased'`, reverse the value remaps (`passed → not_interested`, `purchased → interested`), drop the 3 new columns, drop the `lot_action_history` table. `maybe` is **not recoverable** on downgrade — formerly-`maybe` rows remain `interested`. Documented in the migration docstring and asserted by the roundtrip test.

## State machine

### New module: `src/carbuyer/db/lot_state.py`

Lives in `db/` (not `apps/dashboard/`) because the Discord bot also calls it. Putting it in `dashboard/` would pull FastAPI/Jinja2 into the bot import graph through transitive imports — verified concern from architecture review.

```python
def apply_user_action(
    session: AsyncSession,
    lot: AuctionLot,
    action: UserAction | None,           # None = clear (toggle-off)
    *,
    max_bid_cad: Decimal | None = None,
    source: str,                          # "dashboard" / "discord_bot" / etc.
    now: datetime | None = None,          # injectable for tests
    allow_downgrade: bool = True,
) -> None:
    """Mutate `lot` to reflect `action`, write a row to lot_action_history.
    Caller commits.

    Rules:
      - action=None        → clear user_action and ALL bound fields
                             (max_bid_cad, bid_placed_at, won_at all NULL).
      - action=INTERESTED  → clear max_bid_cad, bid_placed_at, won_at.
      - action=BID_PLACED  → max_bid_cad REQUIRED. Stamps bid_placed_at=now()
                             on entry (when prior state != bid_placed). On
                             re-confirm of an existing bid_placed (raising max),
                             preserves bid_placed_at and overwrites max_bid_cad.
                             Clears won_at.
      - action=PURCHASED   → stamps won_at=now() on entry. On re-confirm,
                             preserves won_at. Clears max_bid_cad and
                             bid_placed_at (purchase ends the bid phase).
      - action=PASSED      → clear max_bid_cad, bid_placed_at, won_at.

    Raises ValueError on BID_PLACED without max_bid_cad.
    Raises ValueError when allow_downgrade=False and current state is one of
      {PURCHASED, BID_PLACED} and target action is INTERESTED, PASSED, or None.
      (Used by Discord legacy-button callers to refuse silent regressions.)

    Always appends a row to lot_action_history with (action, max_bid_cad,
    changed_at=now, source). The history row's max_bid_cad echoes the bid
    amount at transition time when action == BID_PLACED; NULL otherwise —
    that way history alone answers "what max did I commit?" even after the
    lot moves to passed and AuctionLot.max_bid_cad has been cleared.
    """
```

**Transition truth table:**

| From → To | `user_action` | `max_bid_cad` | `bid_placed_at` | `won_at` | History row max_bid_cad |
|---|---|---|---|---|---|
| any → INTERESTED | `interested` | NULL | NULL | NULL | NULL |
| any → BID_PLACED (with amt) | `bid_placed` | `amt` | now() if not already bid_placed, else preserved | NULL | `amt` |
| any → BID_PLACED (no amt) | raises ValueError | — | — | — | — |
| any → PURCHASED | `purchased` | NULL | NULL | now() if not already purchased, else preserved | NULL |
| any → PASSED | `passed` | NULL | NULL | NULL | NULL |
| any → None | NULL | NULL | NULL | NULL | NULL |
| {PURCHASED,BID_PLACED} → {INTERESTED,PASSED,None} when `allow_downgrade=False` | raises ValueError | — | — | — | — |

The `allow_downgrade=False` path is **only** used by Discord legacy-button callbacks (`LotMaybeButton`, `LotNotInterestedButton`) to prevent a tap-on-old-notification from silently regressing a purchased lot back to interested. Dashboard callers pass `allow_downgrade=True` (default) — the operator explicitly clicking Pass on a purchased lot is a legitimate user override.

## Callers

### Writers — go through `apply_user_action`

**`apps/dashboard/routers/actions.py`:**
- Delete lines 26–45 (the alias block).
- `action: str` form field parsed directly: `UserAction(action)` when not toggle-off, `None` when `currently_active=True`. Invalid → 422.
- New optional form field: `max_bid_cad: Decimal | None = Form(None)`. Validation: when posted `action == "bid_placed"` and `max_bid_cad` is None, return 422 *before* hitting `apply_user_action` (so the user gets a clean error, not the ValueError from the state-machine).
- Call: `apply_user_action(session, lot, parsed, max_bid_cad=form_max_bid_cad, source="dashboard")`.
- `effective_state` simplifies — DB and rendered values match. The `partials/action_buttons_fragment.html` template signature does **not** change in this PR. (When the place-bid modal lands, that template will need a `max_bid_cad` context var; called out as a known follow-up break point.)

**`apps/bot/views.py`:**
- `_set_user_action(lot_id, action, *, allow_downgrade)` wraps `apply_user_action(session, lot, action, source="discord_bot", allow_downgrade=allow_downgrade)`. On ValueError from refused downgrade, replies ephemerally: *"Lot already marked purchased/bid placed. Use the dashboard to change state."* Logs the refused interaction.
- `LotInterestedButton.callback` → `_set_user_action(lot_id, UserAction.INTERESTED, allow_downgrade=False)`.
- `LotMaybeButton.callback` → `_set_user_action(lot_id, UserAction.INTERESTED, allow_downgrade=False)`. Class stays registered (regex `template=r"deal:maybe:..."` unchanged) so legacy messages don't 500.
- `LotNotInterestedButton.callback` → `_set_user_action(lot_id, UserAction.PASSED, allow_downgrade=False)`. Same: class stays registered for legacy-message compatibility.

**New notification button row** (in `apps/notifier/discord_post.py`):
- Drop the `"deal:maybe:..."` button entry at line 47. New notifications post two buttons (Interested / Pass). Verify no other call site builds 3-button rows.

### Readers — filter-set updates

| File | Change |
|---|---|
| `apps/auction_distiller/distiller.py:138` + comments at lines 8, 116 | `not_in([INTERESTED, MAYBE])` → `not_in([INTERESTED, BID_PLACED, PURCHASED])`. Retention keeps watched/active lots; only `passed` (and NULL after the keep window) drops. |
| `apps/dashboard/routers/feed.py:236–242` | `[INTERESTED, MAYBE]` → `[INTERESTED, BID_PLACED, PURCHASED]`. `is_distinct_from(NOT_INTERESTED)` → `is_distinct_from(PASSED)`. |
| `apps/dashboard/today_queries.py:32, 34, 150, 202, 223, 362, 367` | `_WATCHED_ACTIONS = (INTERESTED, MAYBE)` → `(INTERESTED, BID_PLACED, PURCHASED)`. Also add `_INTEREST_DERIVATION_ACTIONS = (INTERESTED, BID_PLACED)` — **purchased lots do NOT contribute to derived watched-make/model**, otherwise the user gets perpetual "new lot matching your interests" alerts for vehicles already bought. Verify each `_WATCHED_ACTIONS` usage and replace with `_INTEREST_DERIVATION_ACTIONS` at the make/model derivation site specifically (`derive_watched_make_model` and any other "what is this user interested in shopping for" derivations). Comment at line 32 + 362 rewritten. |
| `apps/dashboard/today_queries.py:274, 318, 354` | `is_distinct_from(NOT_INTERESTED)` → `is_distinct_from(PASSED)`. |
| `apps/notifier/triggers.py:43, 85, 86, 103, 116` | `_WATCHED_ACTIONS = {"interested", "maybe"}` → `{"interested", "bid_placed", "purchased"}` (kept as the broad set — short-circuit below handles purchased specially). |
| `apps/notifier/triggers.py:58` | `"not_interested"` → `"passed"`. |
| **NEW** `apps/notifier/triggers.py` short-circuit | Early-return False from `going_cheap`, `closing_soon`, and `lot_extended` evaluations when `state.user_action == "purchased"`. Rationale: a lot the user already bought should not generate price/closing pings. (`early_warning_notified_at` etc. are already one-shot via the timestamp guard, so re-firing only matters for the rescore-threshold branches.) Implementation: add `if state.user_action == "purchased": return False` at the top of those rule functions. |

**`apps/dashboard/routers/watched.py` — full rewrite of route shape:**
- Drop the `tier` query param and the tier-tabs concept.
- Query all 4 named states (NULL excluded — un-actioned lots don't belong on the watchlist).
- Return a dict with 4 keys: `{"interested": [...], "bid_placed": [...], "purchased": [...], "passed": [...]}`. Pre-partitioned at the router so the template doesn't need Jinja `selectattr` filtering.
- Counts available as `len(items[state])` in template.

**`templates/pages/watched.html` — interim 4-bucket shape:**
- Four `<section>` blocks, one per state. Each section heading shows the state name + count, then renders `partials/lot_card.html` for each item via `{% for item in items.bid_placed %}` etc.
- No tier toggle UI.
- Kanban column layout / drag-to-move is the follow-up PR.

**`templates/partials/feed_filters.html:81–82`:**
- Rename form field `name="exclude_not_interested"` → `name="exclude_passed"`. Update the `{% if filters.exclude_passed %}` template lookup. Update the corresponding route handler in `feed.py` (Pydantic field on the filter model or form param name).
- Label text already reads "Hide passed lots" per the reviewer; verify after rename.

**`static/css/components/lot-card.css:51–52`:**
- `.lot-card[data-state="not_interested"]` → `.lot-card[data-state="passed"]`. Same opacity rules apply to the new state name.
- No new rules for `bid_placed` or `purchased` in this PR (visual treatment for those is the kanban PR's job).
- Check `templates/partials/lot_card.html` to confirm the `data-state` attribute is set from `user_action` directly (no transform).

## Testing

### Unit — `tests/db/test_lot_state.py` (new; was wrongly placed under `tests/dashboard/` in v1 of this spec)

Pure-logic tests against the truth table. In-memory `AuctionLot()` instances (no session for state-mutation assertions; in-memory `Session` for the history-row assertion). Coverage:

- Each truth-table row → one test asserting the resulting `user_action`, `max_bid_cad`, `bid_placed_at`, `won_at` values.
- History row written on every transition including toggle-off.
- BID_PLACED re-confirm preserves `bid_placed_at`, overwrites `max_bid_cad`, writes new history row.
- ValueError on BID_PLACED without `max_bid_cad`.
- ValueError on disallowed-downgrade when `allow_downgrade=False`.
- `now` injected; timestamps deterministic.

### Migration — `tests/db/test_migration_four_state.py` (new)

Using the existing alembic test fixture (located via `find tests -name 'conftest*' -o -name '*alembic*'`; will use the project's actual pattern). Four scenarios:

1. **Upgrade backfill.** Seed rows: `interested`, `maybe`, `not_interested`, `was_purchased_by_us=TRUE` (with `user_action='interested'`), and the **edge case** `was_purchased_by_us=TRUE AND user_action='not_interested'` (admin override scenario). Upgrade head. Assert: `interested`, `interested`, `passed`, `purchased`, `purchased`. The last row specifically asserts the migration-ordering invariant (`purchased` wins over `passed`). `was_purchased_by_us` column is gone.
2. **History seeded.** `lot_action_history` has one row per non-NULL labeled lot, all `source='migration'`.
3. **CHECK enforcement.** Direct `INSERT` raises `IntegrityError` for: `bid_placed` with NULL `max_bid_cad`; `passed` with non-NULL `max_bid_cad`; `purchased` with NULL `won_at`; `interested` with non-NULL `bid_placed_at`. Bidirectional CHECK proved both ways.
4. **Downgrade roundtrip.** Upgrade → downgrade. Assert `was_purchased_by_us` re-exists with the right values; `passed` mapped back to `not_interested`; `lot_action_history` table is gone. Document loss: formerly-`maybe` rows stay `interested` post-roundtrip.

### Caller updates — extend existing test files (correct paths)

| File | Change |
|---|---|
| `tests/apps/dashboard/test_actions.py` | Add: POST `action=bid_placed` + `max_bid_cad=500` writes the right shape and a history row. Missing `max_bid_cad` returns 422 (not 500). Toggle-off clears all bound fields and writes a history row with `user_action=NULL`. |
| `tests/apps/test_distiller.py` | Retention keeps `bid_placed` and `purchased` lots; drops `passed` after the keep-window. |
| `tests/apps/test_notifier_triggers.py` | `_WATCHED_ACTIONS` extension covered. **New test**: `going_cheap` short-circuit on `user_action='purchased'`. Same for `closing_soon` and `lot_extended`. |
| `tests/apps/test_notifier_post.py:112–113` | Assertion at lines 112–113 updated: new notifications carry 2 buttons (`deal:interested:7`, `deal:not_interested:7`), no `deal:maybe:7`. |
| `tests/apps/test_bot_views.py:42–43` | Parametrize table loses the `MAYBE`/`NOT_INTERESTED` enum-write expectations; updated to expect `INTERESTED` from the maybe button and `PASSED` from the not-interested button. New test: `allow_downgrade=False` on a `purchased` lot raises ValueError and the bot sends ephemeral "use the dashboard" reply. |
| `tests/db/test_models.py:67` | Drop `"was_purchased_by_us"` from the expected-columns set for `AuctionLot`. Add `max_bid_cad`, `bid_placed_at`, `won_at`. Add a `lot_action_history` table-existence assertion. |
| **New** `tests/apps/dashboard/test_today_queries.py` (if absent, otherwise extend) | `derive_watched_make_model` does NOT include purchased lots — a seeded `purchased` lot's make/model is NOT in the returned set. |
| **New** `tests/apps/dashboard/test_watched.py` | `/watched` returns the 4-bucket dict shape; each bucket lists only its own state. |

### Explicit non-coverage

- Kanban template polish — follow-up PR.
- Place-bid modal UX — follow-up PR (the `partials/action_buttons_fragment.html` context-dict break point lands then).
- Discord button rehydration after restart — existing pattern trusted.

## File-by-file change list (preview for plan)

```
src/carbuyer/db/enums.py                                       modify
src/carbuyer/db/models.py                                      modify   (AuctionLot + new LotActionHistory)
src/carbuyer/db/lot_state.py                                   new
alembic/versions/<new-rev>_four_state_user_action.py           new
src/carbuyer/apps/dashboard/routers/actions.py                 modify   (drop alias, route through apply_user_action, add max_bid_cad form field)
src/carbuyer/apps/dashboard/routers/watched.py                 modify   (4-bucket shape)
src/carbuyer/apps/dashboard/routers/feed.py                    modify   (filter rename, exclude_passed form param)
src/carbuyer/apps/dashboard/today_queries.py                   modify   (constants + purchased short-circuit on derivation)
src/carbuyer/apps/dashboard/templates/pages/watched.html       modify   (4 sections)
src/carbuyer/apps/dashboard/templates/partials/feed_filters.html modify (exclude_passed)
src/carbuyer/apps/dashboard/static/css/components/lot-card.css modify   ([data-state="passed"])
src/carbuyer/apps/auction_distiller/distiller.py               modify   (retention set + docstrings)
src/carbuyer/apps/bot/views.py                                 modify   (3 buttons rewired, allow_downgrade=False from legacy)
src/carbuyer/apps/notifier/triggers.py                         modify   (rename + purchased short-circuit)
src/carbuyer/apps/notifier/discord_post.py                     modify   (drop maybe button entry)

tests/db/test_lot_state.py                                     new
tests/db/test_migration_four_state.py                          new
tests/db/test_models.py                                        modify   (line 67 column set + new tables)
tests/apps/dashboard/test_actions.py                           modify
tests/apps/dashboard/test_watched.py                           new
tests/apps/dashboard/test_today_queries.py                     modify or new
tests/apps/test_distiller.py                                   modify
tests/apps/test_notifier_triggers.py                           modify   (purchased short-circuit)
tests/apps/test_notifier_post.py                               modify   (2-button assertion at L112–113)
tests/apps/test_bot_views.py                                   modify   (parametrize table, allow_downgrade case)
```

## Out of scope (revisit after this lands)

- Watchlist kanban polish — `/watched` becomes the proper 4-column board with drag-to-move.
- Place-bid modal — captures `max_bid_cad` cleanly; updates `partials/action_buttons_fragment.html` to thread the value through HTMX fragments.
- Auto-detecting `bid_placed` from auctioneer bidder identity — memory `[[auctioneer-bid-number-auto-detect]]`.
- Action-history UI (a small audit panel on the lot detail page reading from `lot_action_history`).
- Auctions / Garage / Admin section content.

## Known break point flagged for the next PR

The HTMX fragment template `partials/action_buttons_fragment.html` currently takes a single `effective_state: str | None` context var (post-PR — once the alias gap is closed). The place-bid modal PR will need to thread `max_bid_cad` (current value, for pre-fill on the "raise max" path) through the same fragment context. That contract extension is **not** done here — the modal PR owns it.
