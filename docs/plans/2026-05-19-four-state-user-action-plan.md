# Four-State `user_action` Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 3-value `UserAction` enum with 4-state workflow (`interested` / `bid_placed` / `purchased` / `passed`), add bid/win tracking columns to `AuctionLot`, introduce `lot_action_history` audit table, and rewire all callers + Discord legacy buttons.

**Architecture:** Single alembic revision lands schema atomically. New `carbuyer/db/lot_state.py` module owns the state-machine logic; every writer routes through `apply_user_action(...)`. Bidirectional CHECK constraints enforce that current-state columns clear on exit. Discord legacy-button callbacks get an `allow_downgrade=False` guard so old "Maybe" buttons can't silently regress purchased lots.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL, pytest-asyncio, discord.py, htmx.

**Spec:** `docs/specs/2026-05-19-four-state-user-action-design.md` (commit `bffeeca`).

**Working notes:**
- Test DB schema is built from `Base.metadata.create_all()` in `tests/conftest.py:46-47` — NOT via alembic. Most tests pick up model changes automatically. The migration test is the exception and uses its own engine.
- Run all tests: `uv run pytest -x`. Run a single file: `uv run pytest tests/db/test_lot_state.py -v`.
- Lint: `uv run ruff check src tests` + `uv run pyright src tests`.
- Default the project keeps tests passing on every commit — see user CLAUDE.md.

---

## Phase 0 — Branch setup

### Task 0.1: Create feature branch

- [ ] **Step 1: Branch off main**

```bash
git checkout main
git pull --ff-only
git checkout -b four-state-user-action
```

- [ ] **Step 2: Sanity check — current tests pass before any changes**

```bash
uv run pytest -x
```

Expected: all green. If anything fails on `main`, stop and surface it before continuing.

---

## Phase 1 — Schema foundation (atomic commit)

This phase lands the model + migration + the unavoidable compile-fixes for callers that reference dropped attributes (`AuctionLot.was_purchased_by_us`, `UserAction.MAYBE`, `UserAction.NOT_INTERESTED`). Without these compile-fixes, Python won't import. The phase is a single commit.

### Task 1.1: Rewrite `UserAction` enum

**Files:**
- Modify: `src/carbuyer/db/enums.py:70-73`

- [ ] **Step 1: Replace the enum**

```python
# src/carbuyer/db/enums.py:70-73

class UserAction(StrEnum):
    INTERESTED = "interested"
    BID_PLACED = "bid_placed"
    PURCHASED = "purchased"
    PASSED = "passed"
```

Remove `MAYBE = "maybe"` and `NOT_INTERESTED = "not_interested"`.

### Task 1.2: Update `AuctionLot` model — new columns, drop `was_purchased_by_us`, tighten enum type, add CHECKs and hybrid properties

**Files:**
- Modify: `src/carbuyer/db/models.py` (AuctionLot region, roughly lines 119–400)

- [ ] **Step 1: Update imports**

Add to the existing imports at the top of `src/carbuyer/db/models.py`:

```python
from sqlalchemy import CheckConstraint, Enum as SAEnum, func
from sqlalchemy.ext.hybrid import hybrid_property

from carbuyer.db.enums import UserAction
```

(If any of these already exist, skip the duplicate.)

- [ ] **Step 2: Tighten `user_action` column type**

Replace the current declaration at `models.py:368`:

```python
# OLD
user_action: Mapped[str | None] = mapped_column(String(16), index=True)

# NEW
user_action: Mapped[UserAction | None] = mapped_column(
    SAEnum(UserAction, native_enum=False, length=16),
    index=True,
)
```

- [ ] **Step 3: Add the three current-state columns**

Place these next to `user_action` in the "Owned by: dashboard (user input)" region of `AuctionLot`:

```python
max_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
bid_placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 4: Drop the `was_purchased_by_us` column from `AuctionLot`**

Delete lines 370–376 of `models.py` (the `was_purchased_by_us` declaration on `AuctionLot`). **Do NOT touch the same-named column on `HistoricalSale` at line 483** — that's a distinct fact with different semantics.

- [ ] **Step 5: Add hybrid properties**

Place inside the `AuctionLot` class body (anywhere after the column definitions):

```python
@hybrid_property
def is_active_bid(self) -> bool:
    return self.user_action == UserAction.BID_PLACED

@hybrid_property
def is_purchased(self) -> bool:
    return self.user_action == UserAction.PURCHASED
```

- [ ] **Step 6: Add the three CHECK constraints to `__table_args__`**

`AuctionLot` already has `__table_args__ = (UniqueConstraint(...))` at roughly line 386. Extend it:

```python
__table_args__ = (
    UniqueConstraint(
        "auction_id",
        "source_lot_id",
        name="uq_auction_lots_auction_source",
    ),
    CheckConstraint(
        "(user_action = 'bid_placed') = (max_bid_cad IS NOT NULL)",
        name="ck_auction_lots_bid_placed_iff_max_bid",
    ),
    CheckConstraint(
        "(user_action = 'bid_placed') = (bid_placed_at IS NOT NULL)",
        name="ck_auction_lots_bid_placed_iff_timestamp",
    ),
    CheckConstraint(
        "(user_action = 'purchased') = (won_at IS NOT NULL)",
        name="ck_auction_lots_purchased_iff_won_at",
    ),
)
```

(Verify the existing `UniqueConstraint` arg list — keep its exact original form; only add the three CheckConstraint rows.)

### Task 1.3: Add new `LotActionHistory` model

**Files:**
- Modify: `src/carbuyer/db/models.py` (append after `AuctionLot`, before `HistoricalSale`)

- [ ] **Step 1: Define the model**

```python
class LotActionHistory(Base):
    """Immutable transition log for AuctionLot.user_action.

    Every call to apply_user_action appends one row. The lot's current
    user_action / max_bid_cad / bid_placed_at / won_at columns reflect the
    *current* state; history columns here reflect the state being entered
    at the moment of transition. Together they answer:
      - "what state is this lot in right now?" → AuctionLot
      - "what was the max bid I committed to before backing out?" → here
      - "when did I first mark this purchased?" → here
    """

    __tablename__ = "lot_action_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("auction_lots.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_action: Mapped[UserAction | None] = mapped_column(
        SAEnum(UserAction, native_enum=False, length=16),
    )
    max_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index(
            "ix_lot_action_history_lot_id_changed_at",
            "lot_id",
            "changed_at",
        ),
    )
```

Notes:
- Not extending `TimestampMixin` — history rows are immutable, single timestamp is honest.
- No `relationship` back-pop on `AuctionLot` — keeps the ORM graph lean. Callers that want history query explicitly.
- The composite index covers single-`lot_id` lookups; no separate `index=True` on `lot_id`.

### Task 1.4: Generate alembic migration

**Files:**
- Create: `alembic/versions/<auto-generated-rev>_four_state_user_action.py`

- [ ] **Step 1: Generate the revision skeleton**

```bash
uv run alembic revision -m "four_state_user_action"
```

Open the new file. Find the `revision`, `down_revision` lines and confirm `down_revision` matches the current head (likely `f6c2e9b81a04`).

- [ ] **Step 2: Replace the body**

```python
"""Replace 3-value user_action with 4-state workflow.

Adds max_bid_cad / bid_placed_at / won_at columns to auction_lots,
creates lot_action_history audit table, remaps existing user_action
values (maybe → interested, not_interested → passed), promotes
was_purchased_by_us=TRUE rows to user_action='purchased', drops
was_purchased_by_us from auction_lots, and adds bidirectional CHECK
constraints binding the three new columns to user_action states.

ORDERING INVARIANT: steps 5–7 (enum remaps) MUST run before step 8
(was_purchased_by_us → purchased promotion). A row with both
was_purchased_by_us=TRUE AND user_action='not_interested' lands at
'purchased', NOT 'passed' — purchased wins. Tested in
tests/db/test_migration_four_state.py.

Downgrade is LOSSY: formerly-`maybe` rows stay `interested` post-roundtrip.

Revision ID: <auto>
Revises: f6c2e9b81a04
Create Date: 2026-05-19
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "<auto>"
down_revision: str | Sequence[str] | None = "f6c2e9b81a04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. lot_action_history audit table
    op.create_table(
        "lot_action_history",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "lot_id",
            sa.BigInteger,
            sa.ForeignKey("auction_lots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_action", sa.String(16)),
        sa.Column("max_bid_cad", sa.Numeric(12, 2)),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_lot_action_history_lot_id_changed_at",
        "lot_action_history",
        ["lot_id", "changed_at"],
    )

    # 2. New current-state columns on auction_lots
    op.add_column(
        "auction_lots",
        sa.Column("max_bid_cad", sa.Numeric(12, 2)),
    )
    op.add_column(
        "auction_lots",
        sa.Column("bid_placed_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "auction_lots",
        sa.Column("won_at", sa.DateTime(timezone=True)),
    )

    # 3. Remap enum values. Ordering matters — see ORDERING INVARIANT above.
    op.execute(
        "UPDATE auction_lots SET user_action = 'interested' "
        "WHERE user_action = 'maybe'"
    )
    op.execute(
        "UPDATE auction_lots SET user_action = 'passed' "
        "WHERE user_action = 'not_interested'"
    )
    # 4. Promote was_purchased_by_us rows to user_action='purchased'.
    #    Stamp won_at from updated_at (best available proxy for "when did we win").
    op.execute(
        "UPDATE auction_lots SET "
        "  user_action = 'purchased', "
        "  won_at = COALESCE(updated_at, now()) "
        "WHERE was_purchased_by_us = TRUE"
    )

    # 5. Seed audit history: one row per labeled lot, source='migration'.
    op.execute(
        "INSERT INTO lot_action_history "
        "  (lot_id, user_action, max_bid_cad, changed_at, source) "
        "SELECT id, user_action, NULL, COALESCE(updated_at, now()), 'migration' "
        "FROM auction_lots WHERE user_action IS NOT NULL"
    )

    # 6. Drop the now-redundant was_purchased_by_us column
    op.drop_column("auction_lots", "was_purchased_by_us")

    # 7. Add bidirectional CHECK constraints
    op.create_check_constraint(
        "ck_auction_lots_bid_placed_iff_max_bid",
        "auction_lots",
        "(user_action = 'bid_placed') = (max_bid_cad IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_auction_lots_bid_placed_iff_timestamp",
        "auction_lots",
        "(user_action = 'bid_placed') = (bid_placed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_auction_lots_purchased_iff_won_at",
        "auction_lots",
        "(user_action = 'purchased') = (won_at IS NOT NULL)",
    )


def downgrade() -> None:
    # Drop CHECKs
    op.drop_constraint("ck_auction_lots_purchased_iff_won_at", "auction_lots")
    op.drop_constraint("ck_auction_lots_bid_placed_iff_timestamp", "auction_lots")
    op.drop_constraint("ck_auction_lots_bid_placed_iff_max_bid", "auction_lots")

    # Re-add was_purchased_by_us, backfill from current state
    op.add_column(
        "auction_lots",
        sa.Column(
            "was_purchased_by_us",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        "UPDATE auction_lots SET was_purchased_by_us = TRUE "
        "WHERE user_action = 'purchased'"
    )

    # Reverse the enum remaps. NOTE: 'maybe' is NOT recoverable — all
    # formerly-maybe rows stay as 'interested'.
    op.execute(
        "UPDATE auction_lots SET user_action = 'not_interested' "
        "WHERE user_action = 'passed'"
    )
    op.execute(
        "UPDATE auction_lots SET user_action = 'interested' "
        "WHERE user_action = 'purchased'"
    )

    # Drop the new state columns
    op.drop_column("auction_lots", "won_at")
    op.drop_column("auction_lots", "bid_placed_at")
    op.drop_column("auction_lots", "max_bid_cad")

    # Drop the audit table
    op.drop_index(
        "ix_lot_action_history_lot_id_changed_at",
        table_name="lot_action_history",
    )
    op.drop_table("lot_action_history")
```

Replace both `<auto>` placeholders with the revision id that `alembic revision` actually generated.

- [ ] **Step 3: Apply the migration to the dev DB**

```bash
uv run alembic upgrade head
```

Expected: no errors. If a row violates a CHECK constraint (unlikely on local dev DB but possible if it has hand-modified data), surface to the user — do NOT silently `--sql` skip it.

### Task 1.5: Repair Python callers broken by the schema change

This is the minimal-compile-fix step. **Do not introduce new behavior here** — just keep imports working. The semantic rewires happen in later phases.

**Files:**
- Modify: `src/carbuyer/apps/auction_distiller/distiller.py` (lines 8, 94, 116, 130, 138)
- Modify: `src/carbuyer/apps/bot/views.py` (lines 84, 91, 108, 110, 117, 119, 126, 143, 145)
- Modify: `src/carbuyer/apps/dashboard/routers/actions.py` (lines 34–45)
- Modify: `src/carbuyer/apps/dashboard/routers/watched.py` (lines 18, 27, 30)
- Modify: `src/carbuyer/apps/dashboard/routers/feed.py` (lines 236–242)
- Modify: `src/carbuyer/apps/dashboard/today_queries.py` (lines 32, 34, 142, 150, 202, 223, 274, 318, 354, 362, 367)
- Modify: `src/carbuyer/apps/notifier/triggers.py` (lines 43, 58, 85, 86, 103, 116)
- Modify: `src/carbuyer/apps/notifier/discord_post.py` (line 47)

- [ ] **Step 1: `distiller.py` — drop `was_purchased_by_us` reference**

Replace line 94 (`was_purchased_by_us=lot.was_purchased_by_us,`): remove this kwarg from the `SoldLot` (or whichever target) constructor — it now lives via `user_action == 'purchased'`. If the target model still has the boolean, set it explicitly: `was_purchased_by_us=(lot.user_action == UserAction.PURCHASED),`.

Replace line 130 (`AuctionLot.was_purchased_by_us.is_(False)`) with `AuctionLot.user_action.is_not(UserAction.PURCHASED)`.

Replace line 138 (`not_in([UserAction.INTERESTED, UserAction.MAYBE])`) with `not_in([UserAction.INTERESTED, UserAction.BID_PLACED, UserAction.PURCHASED])`.

Update docstring at line 8 and comment at line 116: replace `INTERESTED or MAYBE` and `INTERESTED/MAYBE` with `INTERESTED/BID_PLACED/PURCHASED`.

- [ ] **Step 2: `bot/views.py` — mechanical rename**

For each `UserAction.MAYBE` reference: replace with `UserAction.INTERESTED`. (`LotMaybeButton.callback` now writes `INTERESTED` per spec.)

For each `UserAction.NOT_INTERESTED` reference: replace with `UserAction.PASSED`.

**Do NOT yet add the `allow_downgrade` guard** — that goes in Phase 6. This step keeps the module importable.

- [ ] **Step 3: `dashboard/routers/actions.py` — delete the alias block**

Delete lines 26–45 (everything from the comment `# The 4-state workflow ...` through `return UserAction(action).value`). At call sites (lines 76–77), change:

```python
# OLD
db_value = _to_enum_value(action)
lot.user_action = db_value

# NEW (still placeholder — Phase 4 replaces with apply_user_action)
lot.user_action = UserAction(action)
```

Also update `_ACCEPTED_ACTIONS` (line 34) to the new set:

```python
_ACCEPTED_ACTIONS = frozenset({
    "interested", "bid_placed", "purchased", "passed",
})
```

- [ ] **Step 4: `dashboard/routers/watched.py` — temp shim**

Replace lines 17–34 with a stub that compiles. Phase 3 rewrites the whole route. For now:

```python
_VALID_TIERS: frozenset[str] = frozenset(
    {UserAction.INTERESTED.value, UserAction.BID_PLACED.value,
     UserAction.PURCHASED.value, UserAction.PASSED.value},
)
# ... keep the rest of the route the same, just so it compiles.
```

Update the default tier (line 27) from `UserAction.INTERESTED.value` (still valid) — no change actually needed, just verify it compiles.

- [ ] **Step 5: `dashboard/routers/feed.py` — mechanical rename**

Line 237: `[UserAction.INTERESTED.value, UserAction.MAYBE.value]` → `[UserAction.INTERESTED.value, UserAction.BID_PLACED.value, UserAction.PURCHASED.value]`.

Line 242: `UserAction.NOT_INTERESTED.value` → `UserAction.PASSED.value`.

- [ ] **Step 6: `dashboard/today_queries.py` — mechanical rename**

Line 34: `_WATCHED_ACTIONS = (UserAction.INTERESTED.value, UserAction.MAYBE.value)` → `_WATCHED_ACTIONS = (UserAction.INTERESTED.value, UserAction.BID_PLACED.value, UserAction.PURCHASED.value)`.

Lines 274, 318, 354: `is_distinct_from(UserAction.NOT_INTERESTED.value)` → `is_distinct_from(UserAction.PASSED.value)`.

Comments at lines 32, 142, 362: replace `MAYBE` references with `BID_PLACED/PURCHASED`. (Behavioral split — `_INTEREST_DERIVATION_ACTIONS` — happens in Phase 5.)

- [ ] **Step 7: `notifier/triggers.py` — mechanical rename**

Line 43: `_WATCHED_ACTIONS = frozenset({"interested", "maybe"})` → `frozenset({"interested", "bid_placed", "purchased"})`.

Line 58: `state.user_action == "not_interested"` → `state.user_action == "passed"`.

Lines 85–86: `{"interested", "maybe", None}` → `{"interested", "bid_placed", "purchased", None}`; `{"interested", "maybe"}` → `{"interested", "bid_placed", "purchased"}`.

(Purchased short-circuit happens in Phase 5.)

- [ ] **Step 8: `notifier/discord_post.py` — drop the maybe button entry**

Line 47 — locate the button list. Delete the entry whose `custom_id` is `f"deal:maybe:{lot_id}"`. New notifications now carry 2 buttons total (Interested + Not-interested-style — the second one's label may still say "Not interested" in this PR; it writes `PASSED` via the rewired callback in Phase 6. Visual relabel to "Pass" is a follow-up cosmetic).

### Task 1.6: Update `tests/db/test_models.py` to match new schema

**Files:**
- Modify: `tests/db/test_models.py:67`

- [ ] **Step 1: Fix the expected-columns set**

At line 67 of `tests/db/test_models.py`, the assertion lists expected columns for `auction_lots`. Remove `"was_purchased_by_us"`. Add `"max_bid_cad"`, `"bid_placed_at"`, `"won_at"`.

If there's a similar columns-set assertion for other tables that mentions historical data, leave them alone.

- [ ] **Step 2: Add a smoke test for `lot_action_history` existence**

Append to `tests/db/test_models.py`:

```python
def test_lot_action_history_table_present():
    from carbuyer.db.base import Base

    assert "lot_action_history" in Base.metadata.tables
    cols = {c.name for c in Base.metadata.tables["lot_action_history"].columns}
    assert cols == {
        "id", "lot_id", "user_action", "max_bid_cad", "changed_at", "source",
    }
```

### Task 1.7: Verify Phase 1 — full suite

- [ ] **Step 1: Run test suite**

```bash
uv run pytest -x
```

Expected: green. Existing tests may be hitting the new schema (the conftest rebuilds `Base.metadata` on startup, so the new columns + table are present in the test DB).

If anything fails on a callsite that's a *behavior* test (e.g. notifier test asserting on `maybe` button output), let it fail — Phase 5 / 6 will fix those. Capture which tests fail and which pass, and proceed to the next phase only after confirming the failures are in scope for later phases (not new regressions).

Acceptable failing tests at this point: `tests/apps/test_notifier_post.py` (button-count assertion), `tests/apps/test_bot_views.py` (parametrize MAYBE/NOT_INTERESTED), `tests/apps/dashboard/test_actions.py` (alias removal), `tests/apps/test_distiller.py` (retention-set), `tests/apps/test_notifier_triggers.py` (watched-set), watched/today_queries tests if behavioral.

Unacceptable: pyright errors, import errors, schema mismatches, anything in tests/db.

- [ ] **Step 2: Run lint + types**

```bash
uv run ruff check src tests
uv run pyright src tests
```

Both should be clean (you may need to add `# type: ignore` only where the old enum value was a string literal, e.g. raw SQL strings in tests — never in production code).

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "schema: four-state user_action — model + alembic migration + caller compile-fixes"
```

---

## Phase 2 — State-machine module (TDD)

### Task 2.1: Write the state-machine test file (RED)

**Files:**
- Create: `tests/db/test_lot_state.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for carbuyer.db.lot_state.apply_user_action.

Covers the transition truth table from the four-state spec. In-memory
AuctionLot instances are mutated; a fake session captures appended
LotActionHistory rows via session.add().
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from carbuyer.db.enums import UserAction
from carbuyer.db.lot_state import apply_user_action
from carbuyer.db.models import AuctionLot, LotActionHistory

FROZEN = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
EARLIER = datetime(2026, 5, 18, 9, 0, 0, tzinfo=UTC)


class FakeSession:
    """Stand-in for AsyncSession that records session.add() calls."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _lot(**overrides: Any) -> AuctionLot:
    lot = AuctionLot(
        id=1,
        auction_id=1,
        source="hibid",
        source_lot_id="L1",
        url="https://example.com",
    )
    for k, v in overrides.items():
        setattr(lot, k, v)
    return lot


@pytest.mark.parametrize(
    "starting,target,starting_extras,expected",
    [
        # any → INTERESTED clears bound fields
        (None, UserAction.INTERESTED, {}, {
            "user_action": UserAction.INTERESTED,
            "max_bid_cad": None, "bid_placed_at": None, "won_at": None,
        }),
        # INTERESTED → PASSED
        (UserAction.INTERESTED, UserAction.PASSED, {}, {
            "user_action": UserAction.PASSED,
            "max_bid_cad": None, "bid_placed_at": None, "won_at": None,
        }),
        # any → BID_PLACED (with amt) stamps timestamp on first entry
        (UserAction.INTERESTED, UserAction.BID_PLACED, {}, {
            "user_action": UserAction.BID_PLACED,
            "max_bid_cad": Decimal("500"),
            "bid_placed_at": FROZEN,
            "won_at": None,
        }),
        # any → PURCHASED stamps won_at, clears bid fields
        (UserAction.BID_PLACED, UserAction.PURCHASED, {
            "max_bid_cad": Decimal("500"),
            "bid_placed_at": EARLIER,
        }, {
            "user_action": UserAction.PURCHASED,
            "max_bid_cad": None,
            "bid_placed_at": None,
            "won_at": FROZEN,
        }),
        # toggle-off clears everything
        (UserAction.BID_PLACED, None, {
            "max_bid_cad": Decimal("500"),
            "bid_placed_at": EARLIER,
        }, {
            "user_action": None,
            "max_bid_cad": None,
            "bid_placed_at": None,
            "won_at": None,
        }),
    ],
)
def test_transitions(
    starting: UserAction | None,
    target: UserAction | None,
    starting_extras: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    lot = _lot(user_action=starting, **starting_extras)
    session = FakeSession()

    kwargs: dict[str, Any] = {"source": "test", "now": FROZEN}
    if target == UserAction.BID_PLACED:
        kwargs["max_bid_cad"] = Decimal("500")

    apply_user_action(session, lot, target, **kwargs)

    for attr, want in expected.items():
        assert getattr(lot, attr) == want, f"{attr!r}: got {getattr(lot, attr)!r}, want {want!r}"


def test_bid_placed_requires_max_bid() -> None:
    lot = _lot(user_action=UserAction.INTERESTED)
    session = FakeSession()
    with pytest.raises(ValueError, match="max_bid_cad"):
        apply_user_action(
            session, lot, UserAction.BID_PLACED, source="test", now=FROZEN,
        )


def test_bid_placed_reconfirm_preserves_timestamp_overwrites_amount() -> None:
    lot = _lot(
        user_action=UserAction.BID_PLACED,
        max_bid_cad=Decimal("500"),
        bid_placed_at=EARLIER,
    )
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.BID_PLACED,
        max_bid_cad=Decimal("600"), source="test", now=FROZEN,
    )
    assert lot.max_bid_cad == Decimal("600")
    assert lot.bid_placed_at == EARLIER  # preserved


def test_purchased_reconfirm_preserves_won_at() -> None:
    lot = _lot(user_action=UserAction.PURCHASED, won_at=EARLIER)
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.PURCHASED, source="test", now=FROZEN,
    )
    assert lot.won_at == EARLIER  # preserved


def test_allow_downgrade_false_blocks_purchased_to_interested() -> None:
    lot = _lot(user_action=UserAction.PURCHASED, won_at=EARLIER)
    session = FakeSession()
    with pytest.raises(ValueError, match="downgrade"):
        apply_user_action(
            session, lot, UserAction.INTERESTED,
            source="discord_bot", now=FROZEN, allow_downgrade=False,
        )


def test_allow_downgrade_false_blocks_bid_placed_to_passed() -> None:
    lot = _lot(
        user_action=UserAction.BID_PLACED,
        max_bid_cad=Decimal("500"),
        bid_placed_at=EARLIER,
    )
    session = FakeSession()
    with pytest.raises(ValueError, match="downgrade"):
        apply_user_action(
            session, lot, UserAction.PASSED,
            source="discord_bot", now=FROZEN, allow_downgrade=False,
        )


def test_allow_downgrade_false_allows_lateral_purchased_to_purchased() -> None:
    lot = _lot(user_action=UserAction.PURCHASED, won_at=EARLIER)
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.PURCHASED,
        source="discord_bot", now=FROZEN, allow_downgrade=False,
    )
    assert lot.user_action == UserAction.PURCHASED


def test_history_row_appended_on_bid_placed_with_amount() -> None:
    lot = _lot(user_action=UserAction.INTERESTED)
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.BID_PLACED,
        max_bid_cad=Decimal("500"), source="dashboard", now=FROZEN,
    )
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, LotActionHistory)
    assert row.lot_id == 1
    assert row.user_action == UserAction.BID_PLACED
    assert row.max_bid_cad == Decimal("500")
    assert row.changed_at == FROZEN
    assert row.source == "dashboard"


def test_history_row_max_bid_cad_null_when_not_bid_placed() -> None:
    lot = _lot(user_action=UserAction.INTERESTED)
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.PASSED, source="dashboard", now=FROZEN,
    )
    assert len(session.added) == 1
    row = session.added[0]
    assert row.user_action == UserAction.PASSED
    assert row.max_bid_cad is None


def test_history_row_appended_on_toggle_off() -> None:
    lot = _lot(user_action=UserAction.INTERESTED)
    session = FakeSession()
    apply_user_action(session, lot, None, source="dashboard", now=FROZEN)
    assert len(session.added) == 1
    row = session.added[0]
    assert row.user_action is None
    assert row.max_bid_cad is None
```

- [ ] **Step 2: Run the test file — confirm RED**

```bash
uv run pytest tests/db/test_lot_state.py -v
```

Expected: ImportError — `carbuyer.db.lot_state` doesn't exist yet.

### Task 2.2: Implement the state-machine module (GREEN)

**Files:**
- Create: `src/carbuyer/db/lot_state.py`

- [ ] **Step 1: Write the module**

```python
"""State-machine for AuctionLot.user_action transitions.

Single entry point: apply_user_action. Owns the truth-table from the
four-state spec — bid/win field stamping, downgrade guard, audit-log
writes. Callers (dashboard router, Discord bot) commit; this function
only mutates and stages.

Lives in db/ (not apps/dashboard/) so the Discord bot can import it
without pulling FastAPI/Jinja2 through the dashboard package.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from carbuyer.db.enums import UserAction
from carbuyer.db.models import AuctionLot, LotActionHistory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_DOWNGRADE_LOCKED_FROM: frozenset[UserAction] = frozenset({
    UserAction.BID_PLACED, UserAction.PURCHASED,
})
_DOWNGRADE_LOCKED_TO: frozenset[UserAction | None] = frozenset({
    UserAction.INTERESTED, UserAction.PASSED, None,
})


def apply_user_action(
    session: AsyncSession,
    lot: AuctionLot,
    action: UserAction | None,
    *,
    max_bid_cad: Decimal | None = None,
    source: str,
    now: datetime | None = None,
    allow_downgrade: bool = True,
) -> None:
    """Mutate `lot` to reflect `action`, append a LotActionHistory row.

    Caller commits the session. See module docstring + spec for rules.
    """
    when = now or datetime.now(UTC)

    if action == UserAction.BID_PLACED and max_bid_cad is None:
        raise ValueError(
            "BID_PLACED requires max_bid_cad; caller passed None.",
        )

    if (
        not allow_downgrade
        and lot.user_action in _DOWNGRADE_LOCKED_FROM
        and action in _DOWNGRADE_LOCKED_TO
    ):
        raise ValueError(
            f"Refusing downgrade from {lot.user_action!r} to {action!r}: "
            f"allow_downgrade=False (typically a legacy Discord button).",
        )

    prior_action = lot.user_action

    if action is None:
        lot.user_action = None
        lot.max_bid_cad = None
        lot.bid_placed_at = None
        lot.won_at = None
    elif action == UserAction.INTERESTED or action == UserAction.PASSED:
        lot.user_action = action
        lot.max_bid_cad = None
        lot.bid_placed_at = None
        lot.won_at = None
    elif action == UserAction.BID_PLACED:
        lot.user_action = action
        lot.max_bid_cad = max_bid_cad
        if prior_action != UserAction.BID_PLACED:
            lot.bid_placed_at = when
        lot.won_at = None
    elif action == UserAction.PURCHASED:
        lot.user_action = action
        lot.max_bid_cad = None
        lot.bid_placed_at = None
        if prior_action != UserAction.PURCHASED:
            lot.won_at = when

    history_max_bid = (
        max_bid_cad if action == UserAction.BID_PLACED else None
    )
    session.add(
        LotActionHistory(
            lot_id=lot.id,
            user_action=action,
            max_bid_cad=history_max_bid,
            changed_at=when,
            source=source,
        )
    )
```

- [ ] **Step 2: Run the tests — confirm GREEN**

```bash
uv run pytest tests/db/test_lot_state.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Lint + types**

```bash
uv run ruff check src/carbuyer/db/lot_state.py tests/db/test_lot_state.py
uv run pyright src/carbuyer/db/lot_state.py tests/db/test_lot_state.py
```

Both clean.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/db/lot_state.py tests/db/test_lot_state.py
git commit -m "lot_state: apply_user_action state machine with audit-log writes"
```

---

## Phase 3 — Migration test

### Task 3.1: Write migration test with isolated alembic engine

**Files:**
- Create: `tests/db/test_migration_four_state.py`

- [ ] **Step 1: Write the test file**

```python
"""End-to-end test for the four-state migration.

Spins up a separate test database, runs alembic upgrade/downgrade against
it explicitly (the regular conftest builds schema via Base.metadata
.create_all and skips alembic), seeds fixture rows, asserts the result.

Skip if Postgres isn't reachable (CI environments without docker may
not have the local DB).
"""
from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

PREV_HEAD = "f6c2e9b81a04"  # source_alert_state — replace if down_revision drifted


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture
def migration_db() -> Generator[sa.Engine, None, None]:
    """Fresh sync engine pointed at carbuyer_migration_test schema.

    Drops + creates the schema before each test so each scenario starts
    from a known state. Sync because alembic command.* is sync.
    """
    base = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost/carbuyer",
    )
    if base.endswith("/carbuyer"):
        url = base[: -len("/carbuyer")] + "/carbuyer_migration_test"
    else:
        pytest.skip("DATABASE_URL doesn't look like the dev URL")

    # Convert async URL to sync if needed (alembic uses sync engine)
    sync_url = url.replace("+psycopg_async", "+psycopg")
    eng = sa.create_engine(sync_url)
    with eng.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))

    cfg = _alembic_config(sync_url)
    command.upgrade(cfg, PREV_HEAD)  # upgrade to migration *before* ours
    yield eng
    eng.dispose()


def _seed_pre_migration_lots(eng: sa.Engine) -> dict[str, int]:
    """Insert one row per pre-migration scenario. Returns label → row id."""
    ids: dict[str, int] = {}
    with eng.begin() as conn:
        # First an auction row that all lots can FK to
        conn.execute(sa.text("""
            INSERT INTO auctions (id, source, source_auction_id, source_url, title,
                                  scheduled_end_at, status, created_at, updated_at,
                                  schema_version, sources_seen)
            VALUES (1, 'hibid', 'A1', 'https://x.com/a', 'A',
                    now(), 'live', now(), now(), 1, ARRAY['hibid'])
        """))
        scenarios = {
            "interested": ("interested", False),
            "maybe": ("maybe", False),
            "not_interested": ("not_interested", False),
            "purchased_flag_only": (None, True),
            "purchased_with_interested": ("interested", True),
            "purchased_with_not_interested": ("not_interested", True),  # edge!
        }
        for label, (ua, wpbu) in scenarios.items():
            result = conn.execute(sa.text("""
                INSERT INTO auction_lots (
                    auction_id, source, source_lot_id, url,
                    user_action, was_purchased_by_us,
                    created_at, updated_at, schema_version
                ) VALUES (
                    1, 'hibid', :slid, :url,
                    :ua, :wpbu,
                    now(), now(), 1
                ) RETURNING id
            """), {"slid": label, "url": f"https://x.com/{label}", "ua": ua, "wpbu": wpbu})
            ids[label] = result.scalar_one()
    return ids


def test_upgrade_backfill_maps_correctly(migration_db: sa.Engine) -> None:
    ids = _seed_pre_migration_lots(migration_db)

    cfg = _alembic_config(str(migration_db.url))
    command.upgrade(cfg, "head")

    with migration_db.begin() as conn:
        rows = {
            row.id: row.user_action
            for row in conn.execute(sa.text(
                "SELECT id, user_action FROM auction_lots"
            ))
        }
    assert rows[ids["interested"]] == "interested"
    assert rows[ids["maybe"]] == "interested"
    assert rows[ids["not_interested"]] == "passed"
    assert rows[ids["purchased_flag_only"]] == "purchased"
    assert rows[ids["purchased_with_interested"]] == "purchased"
    # The ORDERING INVARIANT scenario: purchased wins over passed.
    assert rows[ids["purchased_with_not_interested"]] == "purchased"


def test_was_purchased_by_us_column_dropped(migration_db: sa.Engine) -> None:
    _seed_pre_migration_lots(migration_db)
    cfg = _alembic_config(str(migration_db.url))
    command.upgrade(cfg, "head")
    inspector = sa.inspect(migration_db)
    cols = {c["name"] for c in inspector.get_columns("auction_lots")}
    assert "was_purchased_by_us" not in cols
    assert "max_bid_cad" in cols
    assert "bid_placed_at" in cols
    assert "won_at" in cols


def test_history_seeded_for_labeled_lots(migration_db: sa.Engine) -> None:
    ids = _seed_pre_migration_lots(migration_db)
    cfg = _alembic_config(str(migration_db.url))
    command.upgrade(cfg, "head")
    with migration_db.begin() as conn:
        history_rows = conn.execute(sa.text(
            "SELECT lot_id, user_action, source FROM lot_action_history "
            "ORDER BY lot_id"
        )).all()
    # Every lot with non-NULL post-migration user_action gets one row.
    expected_lot_ids = sorted(ids.values())
    actual_lot_ids = sorted(r.lot_id for r in history_rows)
    assert actual_lot_ids == expected_lot_ids
    for row in history_rows:
        assert row.source == "migration"


def test_check_rejects_bid_placed_without_max_bid(
    migration_db: sa.Engine,
) -> None:
    cfg = _alembic_config(str(migration_db.url))
    command.upgrade(cfg, "head")
    with migration_db.begin() as conn:
        conn.execute(sa.text("""
            INSERT INTO auctions (id, source, source_auction_id, source_url, title,
                                  scheduled_end_at, status, created_at, updated_at,
                                  schema_version, sources_seen)
            VALUES (1, 'hibid', 'A1', 'https://x.com/a', 'A',
                    now(), 'live', now(), now(), 1, ARRAY['hibid'])
        """))
    with pytest.raises(IntegrityError):
        with migration_db.begin() as conn:
            conn.execute(sa.text("""
                INSERT INTO auction_lots (
                    auction_id, source, source_lot_id, url, user_action,
                    max_bid_cad, bid_placed_at, won_at,
                    created_at, updated_at, schema_version
                ) VALUES (
                    1, 'hibid', 'L1', 'https://x.com/L1', 'bid_placed',
                    NULL, NULL, NULL,
                    now(), now(), 1
                )
            """))


def test_check_rejects_purchased_without_won_at(
    migration_db: sa.Engine,
) -> None:
    cfg = _alembic_config(str(migration_db.url))
    command.upgrade(cfg, "head")
    with migration_db.begin() as conn:
        conn.execute(sa.text("""
            INSERT INTO auctions (id, source, source_auction_id, source_url, title,
                                  scheduled_end_at, status, created_at, updated_at,
                                  schema_version, sources_seen)
            VALUES (1, 'hibid', 'A1', 'https://x.com/a', 'A',
                    now(), 'live', now(), now(), 1, ARRAY['hibid'])
        """))
    with pytest.raises(IntegrityError):
        with migration_db.begin() as conn:
            conn.execute(sa.text("""
                INSERT INTO auction_lots (
                    auction_id, source, source_lot_id, url, user_action,
                    won_at, created_at, updated_at, schema_version
                ) VALUES (
                    1, 'hibid', 'L1', 'https://x.com/L1', 'purchased',
                    NULL, now(), now(), 1
                )
            """))


def test_downgrade_roundtrip(migration_db: sa.Engine) -> None:
    ids = _seed_pre_migration_lots(migration_db)
    cfg = _alembic_config(str(migration_db.url))

    command.upgrade(cfg, "head")
    command.downgrade(cfg, PREV_HEAD)

    inspector = sa.inspect(migration_db)
    cols = {c["name"] for c in inspector.get_columns("auction_lots")}
    assert "was_purchased_by_us" in cols
    assert "max_bid_cad" not in cols
    assert "lot_action_history" not in inspector.get_table_names()

    with migration_db.begin() as conn:
        rows = {
            row.id: (row.user_action, row.was_purchased_by_us)
            for row in conn.execute(sa.text(
                "SELECT id, user_action, was_purchased_by_us FROM auction_lots"
            ))
        }

    # passed → not_interested (reverse maps), purchased → interested + flag set
    assert rows[ids["not_interested"]] == ("not_interested", False)
    assert rows[ids["purchased_flag_only"]] == ("interested", True)
    # Lossy: maybe is NOT recovered — formerly-maybe stays interested.
    assert rows[ids["maybe"]] == ("interested", False)
```

- [ ] **Step 2: Run the migration tests**

```bash
uv run pytest tests/db/test_migration_four_state.py -v
```

Expected: all green. If `_test_url` or the DATABASE_URL handling above doesn't match the project conventions, adjust — look at `tests/conftest.py:21-31` for the dev URL pattern.

- [ ] **Step 3: Lint + types**

```bash
uv run ruff check tests/db/test_migration_four_state.py
uv run pyright tests/db/test_migration_four_state.py
```

- [ ] **Step 4: Commit**

```bash
git add tests/db/test_migration_four_state.py
git commit -m "tests: alembic upgrade/downgrade for four-state user_action"
```

---

## Phase 4 — Rewire `dashboard/routers/actions.py` through `apply_user_action`

### Task 4.1: Rewrite the actions router

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/actions.py`

- [ ] **Step 1: Replace the `mark_lot` handler**

The minimal-compile-fix in Phase 1 left the route working but not yet routed through the state machine. Now wire it through.

Open `src/carbuyer/apps/dashboard/routers/actions.py`. Replace the entire `mark_lot` function body (currently the assignment-style logic at lines 67–123) with this shape:

```python
from decimal import Decimal

from carbuyer.db.lot_state import apply_user_action

# ... (existing imports stay)


@router.post("/lots/{lot_id}/mark", response_model=None)
async def mark_lot(
    request: Request,
    lot_id: int,
    action: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    currently_active: Annotated[bool, Form()] = False,
    max_bid_cad: Annotated[Decimal | None, Form()] = None,
) -> HTMLResponse | Response:
    """Set, toggle, or clear `user_action` for a lot via apply_user_action.

    `action` is the button intent ("interested" / "bid_placed" / "purchased"
    / "passed"). `currently_active=True` treats the click as toggle-off
    (clear to NULL). `max_bid_cad` is REQUIRED when action == "bid_placed".
    """
    if action not in {"interested", "bid_placed", "purchased", "passed"}:
        raise HTTPException(status_code=422, detail=f"invalid action {action!r}")

    if action == "bid_placed" and max_bid_cad is None and not currently_active:
        raise HTTPException(
            status_code=422,
            detail="bid_placed requires max_bid_cad",
        )

    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)

    if currently_active:
        target: UserAction | None = None
    else:
        target = UserAction(action)

    apply_user_action(
        session, lot, target,
        max_bid_cad=max_bid_cad,
        source="dashboard",
    )
    await session.commit()
    await session.refresh(lot)

    log.info(
        "lot marked", lot_id=lot_id, action=action,
        stored=lot.user_action, toggled_off=currently_active,
    )

    effective_state = lot.user_action.value if lot.user_action else None

    if not request.headers.get("HX-Request"):
        return Response(status_code=204)

    hx_target = request.headers.get("HX-Target", "") or ""
    is_button_fragment_target = (
        hx_target.endswith("-desktop") or hx_target.endswith("-mobile")
    )
    if is_button_fragment_target:
        wrapper_class = (
            "decision-card__actions" if hx_target.endswith("-desktop")
            else "bid-console__actions"
        )
        return templates.TemplateResponse(
            request,
            "partials/action_buttons_fragment.html",
            {
                "lot_id": lot.id,
                "target_id": hx_target,
                "wrapper_class": wrapper_class,
                "effective_state": effective_state,
            },
        )

    auction = await session.get(Auction, lot.auction_id)
    return templates.TemplateResponse(
        request,
        "partials/lot_card.html",
        {
            "item": {"lot": lot, "auction": auction},
            "effective_state": effective_state,
        },
    )
```

- [ ] **Step 2: Delete the now-unused alias scaffolding**

If the Phase 1 compile-fix left `_ACCEPTED_ACTIONS` or `_to_enum_value` in the file, delete them — the new handler validates inline. Verify no other module imports them.

- [ ] **Step 3: Extend the actions test**

**Files:**
- Modify: `tests/apps/dashboard/test_actions.py`

Existing tests likely cover the toggle-off case + basic mark. Add:

```python
async def test_mark_bid_placed_writes_history_and_amount(
    client, session, seed_lot,
):
    lot = await seed_lot(user_action="interested")
    resp = await client.post(
        f"/lots/{lot.id}/mark",
        data={"action": "bid_placed", "max_bid_cad": "500"},
    )
    assert resp.status_code in (200, 204)
    await session.refresh(lot)
    assert lot.user_action.value == "bid_placed"
    assert lot.max_bid_cad == Decimal("500")
    assert lot.bid_placed_at is not None

    from carbuyer.db.models import LotActionHistory
    history = (await session.execute(
        select(LotActionHistory).where(LotActionHistory.lot_id == lot.id)
    )).scalars().all()
    assert len(history) == 1
    assert history[0].source == "dashboard"


async def test_mark_bid_placed_without_amount_returns_422(client, seed_lot):
    lot = await seed_lot(user_action="interested")
    resp = await client.post(
        f"/lots/{lot.id}/mark",
        data={"action": "bid_placed"},  # no max_bid_cad
    )
    assert resp.status_code == 422


async def test_toggle_off_clears_all_bound_fields(client, session, seed_lot):
    from decimal import Decimal
    lot = await seed_lot(
        user_action="bid_placed",
        max_bid_cad=Decimal("500"),
        bid_placed_at=datetime.now(UTC),
    )
    resp = await client.post(
        f"/lots/{lot.id}/mark",
        data={"action": "bid_placed", "currently_active": "true"},
    )
    assert resp.status_code in (200, 204)
    await session.refresh(lot)
    assert lot.user_action is None
    assert lot.max_bid_cad is None
    assert lot.bid_placed_at is None
```

If `seed_lot` / `client` / `session` fixtures don't already exist in that file, look at existing tests in the same file for the local fixture pattern and adapt.

- [ ] **Step 4: Run actions tests + lint**

```bash
uv run pytest tests/apps/dashboard/test_actions.py -v
uv run ruff check src/carbuyer/apps/dashboard/routers/actions.py tests/apps/dashboard/test_actions.py
```

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/actions.py tests/apps/dashboard/test_actions.py
git commit -m "actions: route mark_lot through apply_user_action with max_bid_cad form field"
```

---

## Phase 5 — Behavior changes (notifier short-circuit, derivation set, watched route)

### Task 5.1: Notifier short-circuit on purchased

**Files:**
- Modify: `src/carbuyer/apps/notifier/triggers.py`

- [ ] **Step 1: Add the short-circuit**

Locate the rule functions for `going_cheap`, `closing_soon`, `lot_extended` (read the full file — they're typically named `_should_fire_going_cheap` etc., or are inline branches in `evaluate_triggers`). At the very top of each, before any other check:

```python
if state.user_action == "purchased":
    return False  # never alert on lots we already own
```

If the rules are not separated into functions but inline inside `evaluate_triggers`, add one guard before the rescore branch that contains them.

- [ ] **Step 2: Add the test**

**Files:**
- Modify: `tests/apps/test_notifier_triggers.py`

```python
def test_going_cheap_suppressed_when_purchased():
    state = _build_trigger_state(  # use the existing fixture builder pattern
        user_action="purchased",
        price_deal_score=Decimal("0.9"),
        last_cheap_score=Decimal("0.5"),
        # ... whatever other fields the existing _build_trigger_state takes
    )
    assert evaluate_triggers(state).going_cheap is False


def test_closing_soon_suppressed_when_purchased():
    state = _build_trigger_state(
        user_action="purchased",
        # ... fields that would otherwise fire closing_soon
    )
    assert evaluate_triggers(state).closing_soon is False
```

(Use whatever the existing function name + return shape is.)

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/apps/test_notifier_triggers.py -v
git add src/carbuyer/apps/notifier/triggers.py tests/apps/test_notifier_triggers.py
git commit -m "notifier: suppress going_cheap/closing_soon/lot_extended for purchased lots"
```

### Task 5.2: Split `_WATCHED_ACTIONS` vs `_INTEREST_DERIVATION_ACTIONS`

**Files:**
- Modify: `src/carbuyer/apps/dashboard/today_queries.py`

- [ ] **Step 1: Add the narrower constant**

Near the existing `_WATCHED_ACTIONS` (line 34):

```python
# _WATCHED_ACTIONS = lots the user considers "in their world" — shown
# anywhere "watched" is the boundary (Today buckets, /watched route, feed).
_WATCHED_ACTIONS = (
    UserAction.INTERESTED.value,
    UserAction.BID_PLACED.value,
    UserAction.PURCHASED.value,
)

# _INTEREST_DERIVATION_ACTIONS = lots whose make/model contribute to the
# derived "interests" set. PURCHASED is excluded — otherwise the user
# gets perpetual "new lot matching your interests" alerts for vehicles
# they already own.
_INTEREST_DERIVATION_ACTIONS = (
    UserAction.INTERESTED.value,
    UserAction.BID_PLACED.value,
)
```

- [ ] **Step 2: Use the narrower set in derivation sites**

Locate the function(s) that derive watched make/model — typically `derive_watched_make_model` or similar. Replace `_WATCHED_ACTIONS` with `_INTEREST_DERIVATION_ACTIONS` *only* in those call sites. Leave `_WATCHED_ACTIONS` in the watched-count / alerts-since / today-bucket sites.

If you can't immediately tell which site is "derivation" vs "display", err on the safe side and only swap sites whose result feeds a `make/model IN (...)` query that filters *other* lots. Sites that filter the watched lots themselves (e.g. "how many watched lots are closing soon") stay on `_WATCHED_ACTIONS`.

- [ ] **Step 3: Test**

**Files:**
- Modify: `tests/apps/dashboard/test_today_queries.py` (or create if absent)

```python
# tests/apps/dashboard/test_today_queries.py — new file if absent
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.today_queries import derive_watched_make_model
from carbuyer.db.models import Auction, AuctionLot


async def _seed(session: AsyncSession, **lot_kwargs):
    """Adapt to the columns/required fields in the actual AuctionLot."""
    auction = Auction(
        source="hibid", source_auction_id="A1",
        source_url="https://x.com/a", title="A",
        scheduled_end_at=datetime.now(UTC), status="live",
    )
    session.add(auction)
    await session.flush()
    lot = AuctionLot(
        auction_id=auction.id, source="hibid",
        source_lot_id=lot_kwargs.pop("source_lot_id", "L1"),
        url="https://x.com/L1",
        **lot_kwargs,
    )
    session.add(lot)
    await session.flush()
    return lot


@pytest.mark.asyncio
async def test_derive_watched_make_model_excludes_purchased(session):
    await _seed(session, user_action="interested", make="Toyota", model="Camry")
    await _seed(
        session, source_lot_id="L2", user_action="purchased",
        won_at=datetime.now(UTC), make="Honda", model="Civic",
    )
    result = await derive_watched_make_model(session)
    pairs = {(r.make, r.model) for r in result}
    assert ("Toyota", "Camry") in pairs
    assert ("Honda", "Civic") not in pairs
```

Adjust the actual function name (`derive_watched_make_model`) and return-row shape to match what `today_queries.py` exposes — read the file first. The required `AuctionLot` columns vary; add what the model demands (e.g. `description`, `images_url_list`) to satisfy NOT NULLs. The `session` fixture comes from `tests/conftest.py`.

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/apps/dashboard/test_today_queries.py -v
git add src/carbuyer/apps/dashboard/today_queries.py tests/apps/dashboard/test_today_queries.py
git commit -m "today_queries: exclude purchased from derived make/model interests"
```

### Task 5.3: Watched route — 4-bucket shape

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/watched.py`
- Modify: `src/carbuyer/apps/dashboard/templates/pages/watched.html`
- Create: `tests/apps/dashboard/test_watched.py` (if absent)

- [ ] **Step 1: Rewrite the route**

```python
# src/carbuyer/apps/dashboard/routers/watched.py
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.enums import UserAction
from carbuyer.db.models import Auction, AuctionLot

router = APIRouter()

_BUCKET_STATES = (
    UserAction.INTERESTED,
    UserAction.BID_PLACED,
    UserAction.PURCHASED,
    UserAction.PASSED,
)
_PER_BUCKET_LIMIT = 100


@router.get("/watched", response_class=HTMLResponse)
async def watched(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """4-bucket flat list. Kanban polish is the follow-up PR."""
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.user_action.in_([s.value for s in _BUCKET_STATES]))
        .order_by(Auction.scheduled_end_at.asc().nulls_last())
    )
    rows = (await session.execute(stmt)).all()

    buckets: dict[str, list[dict[str, Any]]] = {s.value: [] for s in _BUCKET_STATES}
    for lot, auc in rows:
        key = lot.user_action.value if lot.user_action else None
        if key in buckets and len(buckets[key]) < _PER_BUCKET_LIMIT:
            buckets[key].append({"lot": lot, "auction": auc})

    return templates.TemplateResponse(
        request,
        "pages/watched.html",
        {"buckets": buckets},
    )
```

- [ ] **Step 2: Rewrite the template**

Read the current `src/carbuyer/apps/dashboard/templates/pages/watched.html` to capture the existing layout shell (block name, base extension, macros used). Replace its body with four sections:

```html
{% extends "base.html" %}
{% block nav %}watchlist{% endblock %}
{% block title %}Watchlist · CarBuyer{% endblock %}

{% block content %}
{% from "partials/lot_card.html" import lot_card with context %}
<section class="watched">
  <h1>Watchlist</h1>

  {% for state, label in [
    ("interested", "Interested"),
    ("bid_placed", "Bid placed"),
    ("purchased", "Purchased"),
    ("passed", "Passed"),
  ] %}
    <section class="watched__bucket" data-state="{{ state }}">
      <h2>
        {{ label }}
        <span class="watched__count">{{ buckets[state] | length }}</span>
      </h2>
      {% if buckets[state] %}
        <ul class="watched__list">
          {% for item in buckets[state] %}
            <li>{{ lot_card(item) }}</li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="watched__empty">No lots in this bucket.</p>
      {% endif %}
    </section>
  {% endfor %}
</section>
{% endblock %}
```

(Adjust class names + macro import to match the existing template's macro pattern — read `partials/lot_card.html` to confirm the macro signature.)

- [ ] **Step 3: Test**

```python
# tests/apps/dashboard/test_watched.py
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_watched_returns_four_buckets(client, seed_lot):
    await seed_lot(user_action="interested")
    await seed_lot(user_action="bid_placed", max_bid_cad="500")  # may need timestamp too
    await seed_lot(user_action="purchased")
    await seed_lot(user_action="passed")
    resp = await client.get("/watched")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-state="interested"' in body
    assert 'data-state="bid_placed"' in body
    assert 'data-state="purchased"' in body
    assert 'data-state="passed"' in body
```

(Update `seed_lot` shape to match local fixture conventions; if `bid_placed` requires `max_bid_cad` and `bid_placed_at`, the fixture builder must satisfy the CHECK.)

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/apps/dashboard/test_watched.py -v
git add src/carbuyer/apps/dashboard/routers/watched.py \
        src/carbuyer/apps/dashboard/templates/pages/watched.html \
        tests/apps/dashboard/test_watched.py
git commit -m "watched: 4-bucket flat list shape over the new state machine"
```

### Task 5.4: Feed filter rename — `exclude_not_interested` → `exclude_passed`

**Files:**
- Modify: `src/carbuyer/apps/dashboard/templates/partials/feed_filters.html:81-82`
- Modify: `src/carbuyer/apps/dashboard/routers/feed.py` (filter model / Pydantic schema with the field)

- [ ] **Step 1: Update template**

In `feed_filters.html` change `name="exclude_not_interested"` to `name="exclude_passed"`, and `{% if filters.exclude_not_interested %}` to `{% if filters.exclude_passed %}`. Update the visible label if it still says "not interested" — should read "passed".

- [ ] **Step 2: Update the matching field in `feed.py`**

`feed.py` has a Pydantic filter model (search for `exclude_not_interested` in the file). Rename the field to `exclude_passed`. Update the query branch that consumes it (the comparison value remains `UserAction.PASSED.value`).

- [ ] **Step 3: Update any feed test that posts this form param**

```bash
grep -rn "exclude_not_interested" /home/markbohaychuk/repos/CarBuyerAssistant/tests
```

Replace in every match.

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/apps/dashboard/ -v -k feed
git add -A
git commit -m "feed: rename exclude_not_interested → exclude_passed"
```

### Task 5.5: CSS — `data-state="not_interested"` → `data-state="passed"`

**Files:**
- Modify: `src/carbuyer/apps/dashboard/static/css/components/lot-card.css:51-52`

- [ ] **Step 1: Rename the selector**

```css
.lot-card[data-state="passed"] { opacity: 0.55; }
.lot-card[data-state="passed"]:hover { opacity: 0.95; }
```

- [ ] **Step 2: Verify the data-state value source**

```bash
grep -n 'data-state' /home/markbohaychuk/repos/CarBuyerAssistant/src/carbuyer/apps/dashboard/templates/partials/lot_card.html
```

Confirm the template emits `data-state="{{ lot.user_action }}"` or `data-state="{{ lot.user_action.value }}"`. If it stringifies the enum (which Python's StrEnum does to its value automatically), no template change needed. If it uses the bare attribute name, double-check.

- [ ] **Step 3: Rebuild CSS**

```bash
make css
```

(Tailwind v4 compile step. If `make css` fails because tailwind isn't installed, `make tailwind-install` first.)

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/apps/dashboard/static/css/components/lot-card.css \
        src/carbuyer/apps/dashboard/static/css/app.css
git commit -m "lot-card css: rename data-state selector not_interested → passed"
```

---

## Phase 6 — Discord bot rewire (legacy buttons + allow_downgrade)

### Task 6.1: Bot views — apply_user_action + allow_downgrade guard

**Files:**
- Modify: `src/carbuyer/apps/bot/views.py`

- [ ] **Step 1: Replace `_set_user_action`**

```python
from carbuyer.db.lot_state import apply_user_action

async def _set_user_action(
    lot_id: int,
    action: UserAction,
    *,
    allow_downgrade: bool = True,
) -> tuple[bool, str | None]:
    """Returns (ok, refusal_reason). ok=False with a reason means the
    state machine refused the transition (typically a legacy-button
    downgrade). ok=False with reason=None means the lot was missing.
    """
    async with get_session() as session, session.begin():
        lot = await session.get(AuctionLot, lot_id)
        if lot is None:
            log.warning(
                "user_action write skipped — lot not found",
                lot_id=lot_id, action=action,
            )
            return False, None
        try:
            apply_user_action(
                session, lot, action,
                source="discord_bot",
                allow_downgrade=allow_downgrade,
            )
        except ValueError as exc:
            log.info(
                "user_action write refused", lot_id=lot_id,
                action=action, reason=str(exc),
            )
            return False, str(exc)
        log.info("user_action written", lot_id=lot_id, action=action)
        return True, None
```

- [ ] **Step 2: Update each button's callback to use the new return shape and `allow_downgrade=False`**

For each of `LotInterestedButton`, `LotMaybeButton`, `LotNotInterestedButton`, update the callback to:

```python
async def callback(self, interaction: Interaction) -> Any:
    await interaction.response.defer(ephemeral=True)
    ok, refusal = await _set_user_action(
        self.lot_id, UserAction.<TARGET>,  # INTERESTED / INTERESTED / PASSED
        allow_downgrade=False,
    )
    if ok:
        msg = f"Marked lot {self.lot_id} as <label>."  # interested / interested / passed
    elif refusal is not None:
        msg = (
            f"Lot {self.lot_id} is already marked purchased or bid-placed. "
            f"Use the dashboard to change its state."
        )
    else:
        msg = f"Lot {self.lot_id} not found."
    await interaction.followup.send(msg, ephemeral=True)
```

Concrete mapping:
- `LotInterestedButton.callback` → `UserAction.INTERESTED`, label `interested`.
- `LotMaybeButton.callback` → `UserAction.INTERESTED`, label `interested` (legacy-message intent merges into INTERESTED). The class stays registered with `template=r"deal:maybe:..."`.
- `LotNotInterestedButton.callback` → `UserAction.PASSED`, label `passed`. Class stays registered with `template=r"deal:not_interested:..."`.

- [ ] **Step 3: Update bot tests**

**Files:**
- Modify: `tests/apps/test_bot_views.py:42-43`

Replace the parametrize table to expect the new write values:

```python
@pytest.mark.parametrize(
    "button_cls,expected_action",
    [
        (LotInterestedButton, UserAction.INTERESTED),
        (LotMaybeButton, UserAction.INTERESTED),       # legacy → INTERESTED
        (LotNotInterestedButton, UserAction.PASSED),   # legacy → PASSED
    ],
)
```

Add a test for the refuse-downgrade path:

```python
async def test_legacy_maybe_refuses_downgrade_of_purchased_lot(
    seed_lot, mock_interaction,
):
    lot = await seed_lot(user_action="purchased")
    button = LotMaybeButton(lot.id)
    interaction = mock_interaction()
    await button.callback(interaction)
    # ephemeral message indicates refusal, not success
    msg = interaction.followup.send.call_args.args[0]
    assert "purchased" in msg.lower() or "bid" in msg.lower()
    # state didn't regress
    await session.refresh(lot)
    assert lot.user_action == UserAction.PURCHASED
```

(Adapt to local fixture conventions for `mock_interaction` / `seed_lot`.)

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/apps/test_bot_views.py -v
git add src/carbuyer/apps/bot/views.py tests/apps/test_bot_views.py
git commit -m "bot: route buttons through apply_user_action with allow_downgrade=False for legacy"
```

### Task 6.2: Notifier post — 2-button assertion fix

**Files:**
- Modify: `tests/apps/test_notifier_post.py:112-113`

- [ ] **Step 1: Update the test**

Find the assertion that lists expected button `custom_id`s (around line 112–113). Drop the `deal:maybe:` entry. The expected list should now be:

```python
expected_custom_ids = {
    f"deal:interested:{lot_id}",
    f"deal:not_interested:{lot_id}",
}
```

If the test does ordered assertions, update accordingly.

- [ ] **Step 2: Run + commit**

```bash
uv run pytest tests/apps/test_notifier_post.py -v
git add tests/apps/test_notifier_post.py
git commit -m "tests: notifier_post — new notifications carry 2 buttons not 3"
```

---

## Phase 7 — Sweep + verify

### Task 7.1: Grep for stragglers

- [ ] **Step 1: Verify nothing references dropped enum values**

```bash
grep -rn "UserAction.MAYBE\|UserAction.NOT_INTERESTED" /home/markbohaychuk/repos/CarBuyerAssistant/src /home/markbohaychuk/repos/CarBuyerAssistant/tests
```

Expected: no matches. Anything that comes back must be remediated before continuing.

- [ ] **Step 2: Verify `was_purchased_by_us` references are scoped to HistoricalSale**

```bash
grep -rn "was_purchased_by_us" /home/markbohaychuk/repos/CarBuyerAssistant/src /home/markbohaychuk/repos/CarBuyerAssistant/tests
```

Expected matches:
- `db/models.py` — only the `HistoricalSale` column (around line 483).
- Anywhere that explicitly handles HistoricalSale (e.g. ingester for comp data) — should still be there.
- Migration file — downgrade path.

Unexpected matches (anywhere referencing `AuctionLot.was_purchased_by_us`) must be remediated.

- [ ] **Step 3: Verify string-literal stragglers**

```bash
grep -rn '"maybe"\|"not_interested"' /home/markbohaychuk/repos/CarBuyerAssistant/src
```

Acceptable matches:
- `bot/views.py` — `template=r"deal:maybe:(?P<lot_id>\d+)"` (regex for legacy custom_ids — KEEP).
- `bot/views.py` — `template=r"deal:not_interested:(?P<lot_id>\d+)"` (same — KEEP).
- Comments / docstrings explaining the legacy mapping.
- Migration file body.

Anywhere else is a straggler — fix.

### Task 7.2: Full verification

- [ ] **Step 1: Full test suite**

```bash
uv run pytest -x
```

Expected: all green.

- [ ] **Step 2: Lint + types**

```bash
uv run ruff check src tests
uv run pyright src tests
```

Both clean.

- [ ] **Step 3: Boot the dashboard once and click through**

```bash
uv run python -m carbuyer.apps.dashboard &
DASH_PID=$!
sleep 3
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/watched
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/lots
kill $DASH_PID
```

Expected: 200 for all three. If `/watched` 500s, inspect the rendered HTML for the trace.

If you have manual access, open the dashboard and:
- Mark a lot as `bid_placed` with an amount — verify the lot now shows up in the "Bid placed" bucket on `/watched`.
- Toggle the same button again to clear — verify it disappears from the bucket.
- Confirm a notification button row shows two buttons, not three (visible only on freshly-posted notifications — historical messages will still have three).

### Task 7.3: Memory + final commit

**Files:**
- Modify: `/home/markbohaychuk/.claude/projects/-home-markbohaychuk-repos-CarBuyerAssistant/memory/dashboard-redesign-direction-a.md`

- [ ] **Step 1: Mark this PR done in memory**

Append a "Status" line near the top of the memory file:

```
**Status (2026-05-19):** Four-state user_action migration landed. UserAction enum is now 4-value; AuctionLot has max_bid_cad / bid_placed_at / won_at columns; lot_action_history audit table seeded; all callers route through carbuyer.db.lot_state.apply_user_action. Next: watchlist kanban polish, then place-bid modal.
```

(Don't commit this change to the repo — memory files live outside the repo.)

- [ ] **Step 2: Final sanity**

```bash
git log --oneline main..HEAD
```

Read the commit list — each one should be a clean atomic step. If any one is a massive everything-commit, split it before opening the PR.

- [ ] **Step 3: Push branch**

```bash
git push -u origin four-state-user-action
```

- [ ] **Step 4: PR creation (await user confirmation before running)**

Per the user CLAUDE.md, do NOT open the PR autonomously — ask the user first. Suggested PR body:

> Replaces 3-value user_action enum (interested/maybe/not_interested) with 4-state workflow (interested/bid_placed/purchased/passed). Adds max_bid_cad/bid_placed_at/won_at columns, lot_action_history audit table, bidirectional CHECK constraints, and routes every writer through carbuyer.db.lot_state.apply_user_action. Discord legacy buttons (Maybe / Not interested) keep working on old messages but refuse to downgrade purchased/bid_placed lots. Notifier no longer fires going_cheap or closing_soon for purchased lots; today_queries derives "interest" make/models from interested + bid_placed (not purchased) so the user doesn't get perpetual alerts for vehicles already bought.

---

## Summary of phase-level success criteria

| Phase | Done when |
|---|---|
| 0 | `four-state-user-action` branch exists; baseline tests green. |
| 1 | Migration applied, model + enums updated, all imports resolve, schema-level tests green. Some behavioral tests may still fail (deferred to later phases). |
| 2 | `lot_state.py` exists with full truth-table coverage; `test_lot_state.py` green. |
| 3 | `test_migration_four_state.py` exercises upgrade backfill, ordering invariant, CHECKs, downgrade roundtrip — all green. |
| 4 | `/lots/{id}/mark` routes through `apply_user_action`; `test_actions.py` covers bid_placed with amount, 422 without, toggle-off clears all bound fields. |
| 5 | Notifier suppresses on purchased; `_INTEREST_DERIVATION_ACTIONS` split applied; watched route returns 4 buckets; feed filter renamed; CSS updated. |
| 6 | Discord bot writes go through apply_user_action; legacy buttons refuse downgrade; `test_bot_views.py` covers the refusal path; notifier_post test asserts 2 buttons. |
| 7 | Greps for old enum values come back clean (except the legacy regex templates); full suite + ruff + pyright clean; manual smoke test of `/`, `/watched`, `/lots` returns 200. |
