# Four-State `user_action` Migration — Design

Spec for the dashboard-redesign foundation work: replace the existing 3-value `user_action` enum with a 4-state workflow and add the bid/win tracking columns the rest of the redesign assumes exist.

Decisions feeding this spec are in the memory file `dashboard-redesign-direction-a` (locked 2026-05-19).

## Goals

- Replace `UserAction` (`interested` / `maybe` / `not_interested`) with the 4-state workflow (`interested` / `bid_placed` / `purchased` / `passed`).
- Add the columns the workflow requires: `max_bid_cad`, `bid_placed_at`, `won_at`.
- Remove the transitional alias hack in `apps/dashboard/routers/actions.py` (lines 26–45), where forward-looking action names are stored as legacy enum values.
- Collapse `was_purchased_by_us` into `user_action='purchased'` — one fact, one column.
- Keep persistent Discord buttons in posted notification history working — no "interaction failed" on legacy messages.

## Non-goals (deliberate, follow-up PRs)

- The watchlist kanban UI polish on `/watched`. This PR ships a single-list 4-bucket view; the proper kanban layout is a follow-up.
- The place-bid modal UX. The data model and CHECK constraint land here; the modal lands next.
- Auctions, Garage, Admin section content — explicitly expansion per the memory.

## Decisions feeding the design

| Question | Answer |
|---|---|
| Backfill | Lossy. `maybe → interested`, `not_interested → passed`, `was_purchased_by_us=true → purchased`. |
| Scope | Full migration in one alembic revision: enum + all three new columns + drop `was_purchased_by_us`. |
| `was_purchased_by_us` | Dropped. `user_action='purchased'` is the source of truth. |
| Discord `LotMaybeButton` on legacy messages | Class stays registered; callback writes `INTERESTED`. New notifications post 2 buttons (Interested / Pass), not 3. |
| `max_bid_cad` invariant | DB-level CHECK: `(user_action <> 'bid_placed') OR (max_bid_cad IS NOT NULL)`. |
| Historical bid/won fields when leaving `bid_placed` / `purchased` | Preserved. `max_bid_cad`, `bid_placed_at`, `won_at` are historical record. The state machine never clears them. |

## Schema

### `db/enums.py` — UserAction rewrite

```python
class UserAction(StrEnum):
    INTERESTED = "interested"
    BID_PLACED = "bid_placed"
    PURCHASED = "purchased"
    PASSED = "passed"
```

The DB column stays `String(16)`. Longest value (`"bid_placed"`) is 10 chars.

### `db/models.py` — AuctionLot changes

Add (all nullable):

```python
max_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
bid_placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Drop `was_purchased_by_us`.

Add table-level CHECK constraint:

```python
CheckConstraint(
    "(user_action <> 'bid_placed') OR (max_bid_cad IS NOT NULL)",
    name="ck_auction_lots_bid_placed_has_amount",
)
```

The timestamps (`bid_placed_at`, `won_at`) intentionally have no CHECK — they're app-set metadata, not invariants. The state-machine module sets them; admin scripts may null them.

### Alembic migration — single revision, single transaction

```
1. ADD COLUMN max_bid_cad NUMERIC(12, 2)
2. ADD COLUMN bid_placed_at TIMESTAMPTZ
3. ADD COLUMN won_at TIMESTAMPTZ
4. UPDATE auction_lots SET user_action = 'interested' WHERE user_action = 'maybe'
5. UPDATE auction_lots SET user_action = 'passed'     WHERE user_action = 'not_interested'
6. UPDATE auction_lots SET user_action = 'purchased'  WHERE was_purchased_by_us = TRUE
7. DROP COLUMN was_purchased_by_us
8. ADD CONSTRAINT ck_auction_lots_bid_placed_has_amount
   CHECK ((user_action <> 'bid_placed') OR (max_bid_cad IS NOT NULL))
```

Downgrade is symmetric and lossy: drop CHECK, re-add `was_purchased_by_us` (default false), backfill `TRUE` where `user_action='purchased'`, reverse the value remaps (`passed → not_interested`, `purchased → interested`), drop the 3 new columns. `maybe` is **not recoverable** on downgrade — all formerly-`maybe` rows remain `interested`. This is documented in the migration's docstring and asserted by a roundtrip test.

## State machine

### New module: `src/carbuyer/apps/dashboard/lot_state.py`

Lives in the dashboard app because the dashboard owns the workflow policy. The Discord bot is a downstream consumer; it imports `apply_user_action` rather than co-owning the rules.

Single public function:

```python
def apply_user_action(
    lot: AuctionLot,
    action: UserAction | None,           # None = clear
    *,
    max_bid_cad: Decimal | None = None,
    now: datetime | None = None,         # injectable for tests
) -> None:
    """Mutate `lot` to reflect `action`. Caller commits.

    Rules:
      - action=None        → user_action = NULL (toggle-off / clear).
                             Historical fields (max_bid_cad, bid_placed_at, won_at)
                             are preserved.
      - action=BID_PLACED  → max_bid_cad is REQUIRED. Stamps bid_placed_at=now()
                             if currently NULL; preserves existing bid_placed_at
                             on re-confirms.
      - action=PURCHASED   → stamps won_at=now() if currently NULL; preserves
                             existing won_at on re-confirms.
      - action=INTERESTED  → no field stamping. Historical fields preserved.
      - action=PASSED      → no field stamping. Historical fields preserved.

    Raises ValueError on BID_PLACED without max_bid_cad (defense-in-depth
    against the DB CHECK constraint).
    """
```

Truth table for the transitions:

| Incoming `action` | `user_action` write | `bid_placed_at` | `won_at` | `max_bid_cad` |
|---|---|---|---|---|
| `None` (clear) | NULL | unchanged | unchanged | unchanged |
| `INTERESTED` | `interested` | unchanged | unchanged | unchanged |
| `BID_PLACED` (with `max_bid_cad`) | `bid_placed` | set to `now()` if NULL, else unchanged | unchanged | overwritten with new value |
| `BID_PLACED` (no `max_bid_cad`) | raises `ValueError` | — | — | — |
| `PURCHASED` | `purchased` | unchanged | set to `now()` if NULL, else unchanged | unchanged |
| `PASSED` | `passed` | unchanged | unchanged | unchanged |

## Callers

### Writers — go through `apply_user_action`

- `apps/dashboard/routers/actions.py`
  - Delete the alias block at lines 26–45 (`_ACCEPTED_ACTIONS`, `_to_enum_value`, the transitional comment).
  - Parse form `action` directly into `UserAction(action)` (raises on invalid → 422). If `currently_active=True`, pass `None`.
  - Accept new optional form field `max_bid_cad: Decimal | None` (used only for `BID_PLACED`).
  - Call `apply_user_action(lot, parsed, max_bid_cad=form_value)`.
  - `effective_state` template variable simplifies — DB state and rendered state now match, no alias gap to bridge.

- `apps/bot/views.py`
  - `_set_user_action(lot_id, action)` becomes a thin wrapper over `apply_user_action(lot, action)` (no `max_bid_cad` — Discord buttons don't capture amounts).
  - `LotMaybeButton.callback` continues to fire `apply_user_action(lot, UserAction.INTERESTED)`. The class stays registered; the regex `template=r"deal:maybe:(?P<lot_id>\d+)"` is unchanged so legacy messages keep working.
  - The site that builds the button row for *new* notifications drops `LotMaybeButton`. Result: new notifications carry two buttons (Interested / Pass).

- `apps/notifier/discord_post.py:47`
  - Remove the `"custom_id": f"deal:maybe:{lot_id}"` entry from the new-notification button list. Same reasoning as above.

### Readers — mechanical updates to filter sets

| File | Change |
|---|---|
| `apps/auction_distiller/distiller.py:138` | `not_in([INTERESTED, MAYBE])` → `not_in([INTERESTED, BID_PLACED, PURCHASED])`. Retention rule becomes "keep anything not `passed` and not NULL". Update docstring at line 8 + comment at line 116 to say `INTERESTED/BID_PLACED/PURCHASED` instead of `INTERESTED/MAYBE`. |
| `apps/dashboard/routers/watched.py` | Drop the tier-tabs concept entirely. New shape: one query that fetches all 4 states (including NULL? no — only the 4 named states), partitions in-template into 4 buckets. The `tier` query param is removed. |
| `apps/dashboard/routers/feed.py:236–242` | `[INTERESTED, MAYBE]` → `[INTERESTED, BID_PLACED, PURCHASED]`. `is_distinct_from(NOT_INTERESTED)` → `is_distinct_from(PASSED)`. |
| `apps/dashboard/today_queries.py:34` | `_WATCHED_ACTIONS = (INTERESTED, MAYBE)` → `_WATCHED_ACTIONS = (INTERESTED, BID_PLACED, PURCHASED)`. Comment at line 32 + 362 updated similarly. |
| `apps/dashboard/today_queries.py:274, 318, 354` | `is_distinct_from(NOT_INTERESTED)` → `is_distinct_from(PASSED)`. |
| `apps/notifier/triggers.py:43` | `_WATCHED_ACTIONS = frozenset({"interested", "maybe"})` → `frozenset({"interested", "bid_placed", "purchased"})`. |
| `apps/notifier/triggers.py:58` | `state.user_action == "not_interested"` → `"passed"`. |
| `apps/notifier/triggers.py:85–86` | `{"interested", "maybe", None}` → `{"interested", "bid_placed", "purchased", None}`; `{"interested", "maybe"}` → `{"interested", "bid_placed", "purchased"}`. |

### Watched route — single-list 4-bucket interim shape

`templates/pages/watched.html` is updated to render four sections (one per state), each with a count and a list of lots. No tier-toggle. The kanban polish (proper column layout, drag-to-move, etc.) is the follow-up PR. This interim shape avoids 500ing on the route while not over-investing before the kanban design is finalized.

## Testing

### Unit — `tests/dashboard/test_lot_state.py` (new)

Pure-logic tests. Each row of the truth table above gets one test. `AuctionLot` instantiated in memory, no session. `now` passed explicitly as a frozen datetime so timestamps are deterministic. Covers the historical-preservation rules: re-confirm doesn't bump `bid_placed_at`; moving from `bid_placed` to `passed` keeps `max_bid_cad`; clearing to NULL keeps history.

### Migration — `tests/db/test_migration_four_state.py` (new)

Uses the existing alembic test fixture (locate first to match project pattern):

1. **Upgrade backfill.** Seed 4 rows (`interested`, `maybe`, `not_interested`, `was_purchased_by_us=True`), upgrade head, assert resulting `user_action` values are `interested`, `interested`, `passed`, `purchased` and `was_purchased_by_us` column is gone.
2. **CHECK enforcement.** After upgrade, `INSERT` with `user_action='bid_placed'` and `max_bid_cad=NULL` raises `IntegrityError`.
3. **Downgrade roundtrip.** Upgrade → downgrade. Assert `was_purchased_by_us` re-exists, the `purchased` row has it `TRUE`, the `passed` row maps back to `not_interested`. Assert the formerly-`maybe` row is `interested` post-roundtrip (documents the lossy downgrade).

### Caller updates — extend existing test files

- `tests/dashboard/test_actions.py` (if absent, create) — POST `action=bid_placed` + `max_bid_cad=500` writes the right shape; missing `max_bid_cad` → 422; toggle-off preserves historical fields.
- `tests/auction_distiller/test_distiller.py` — retention keeps `bid_placed` and `purchased` lots; drops `passed` after the keep-window.
- `tests/notifier/test_triggers.py` — `_WATCHED_ACTIONS` extension verified by adding `bid_placed` / `purchased` cases.
- `tests/dashboard/test_today_queries.py` — watched-set extension covered.
- `tests/dashboard/test_watched.py` — single endpoint test for the new 4-bucket shape.
- `tests/bot/test_views.py` (if absent, create) — `LotMaybeButton.callback` writes `INTERESTED`; new-notification button row contains 2 buttons.

### Explicit non-coverage

- Kanban template polish — follow-up PR.
- Place-bid modal — follow-up PR.
- Discord button rehydration after bot restart — existing behavior trusted; only the written value changes.

## File-by-file change list (preview for plan)

```
src/carbuyer/db/enums.py                                 modify
src/carbuyer/db/models.py                                modify
alembic/versions/<new-rev>_four_state_user_action.py     new
src/carbuyer/apps/dashboard/lot_state.py                 new
src/carbuyer/apps/dashboard/routers/actions.py           modify
src/carbuyer/apps/dashboard/routers/watched.py           modify
src/carbuyer/apps/dashboard/routers/feed.py              modify
src/carbuyer/apps/dashboard/today_queries.py             modify
src/carbuyer/apps/dashboard/templates/pages/watched.html modify
src/carbuyer/apps/auction_distiller/distiller.py         modify
src/carbuyer/apps/bot/views.py                           modify
src/carbuyer/apps/notifier/triggers.py                   modify
src/carbuyer/apps/notifier/discord_post.py               modify
tests/dashboard/test_lot_state.py                        new
tests/db/test_migration_four_state.py                    new
tests/dashboard/test_actions.py                          modify or new
tests/dashboard/test_watched.py                          modify or new
tests/dashboard/test_today_queries.py                    modify
tests/auction_distiller/test_distiller.py                modify
tests/notifier/test_triggers.py                          modify
tests/bot/test_views.py                                  modify or new
```

## Out of scope (revisit after this lands)

- Watchlist kanban polish (`/watched` becomes the proper 4-column board).
- Place-bid modal UX with `max_bid_cad` input.
- Auto-detecting `bid_placed` from auctioneer bidder identity — see memory `[[auctioneer-bid-number-auto-detect]]`.
- Auctions / Garage / Admin section content.
