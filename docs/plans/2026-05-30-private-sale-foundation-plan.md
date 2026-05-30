# Private-Sale Foundation Implementation Plan (PR-1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the data + matching foundation for private-sale intake: a new
`private_listings` table and the source-agnostic `adapt_private_listing`
adapter that lets PR-2's saved-search matcher treat a private listing exactly
like an auction lot.

**Architecture:** Private listings live in their own table (NOT `AuctionLot`) —
clean separation from auctions, no bids/premium/closing-time. The matcher built
in PR-2 is already polymorphic (`saved_search_matches.source_kind/source_id`);
this PR adds the `private_listing` adapter so a persisted listing maps to the
existing pure `MatchableListing`/`match_listing`. No scraper, no worker, no
dashboard yet (PR-2/PR-3).

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (async), Alembic, pytest, ruff,
pyright (strict). Tests build schema from `Base.metadata.create_all()` (NOT
Alembic), so a new model is visible to tests as soon as it's imported by
`models.py`.

**Spec:** `docs/specs/2026-05-30-private-sale-intake-design.md`.

---

## Context the implementer must know

- **Models** live in `src/carbuyer/db/models.py`, inherit `Base` (+
  `TimestampMixin` where mutable) from `src/carbuyer/db/base.py`. Style:
  `Mapped[...] = mapped_column(...)`; enum-like columns are `String(N)` (compare
  to `.value`); `text[]` is `ARRAY(Text)` with
  `server_default=text("'{}'::text[]")`; JSONB lists use
  `server_default=text("'[]'::jsonb")`; money is `Numeric(12, 2)`; floats are
  bare `mapped_column()` (double precision); PKs are `BigInteger`. `Index` /
  `text` / `ARRAY` / `JSONB` are already imported in `models.py`.
- **The matcher** (built in PR-2) is in `src/carbuyer/db/saved_searches.py`:
  the `MatchableListing` frozen dataclass, `match_listing(listing, search)`
  (pure), and `adapt_auction_lot(lot, auction) -> MatchableListing`. This PR
  adds a sibling `adapt_private_listing`. `MatchableListing`'s fields are:
  `source_kind, source_id, make, model, year, trim, mileage_km, title_status,
  condition_categorical, province, all_in_cost_cad (int|None), rarity_score`.
  `adapt_auction_lot` rounds the Decimal all-in cost UP via `math.ceil` (a cap
  must not be undercut) — the private adapter mirrors this.
- **Tests** run against `carbuyer_test`; the per-test `session` fixture rolls
  back. Pure adapter tests construct ORM objects in memory (no session), the
  same way `tests/db/test_saved_search_matcher.py` constructs `SavedSearch` and
  `MatchableListing`.
- **Migrations** are hand-written (NOT `--autogenerate`). `down_revision` is the
  current head (find via `uv run alembic heads`).
- **Deferred to PR-2/PR-3** (do NOT build here): the Kijiji scraper +
  `RawPrivateListing`, the `private_sale` worker, enrichment/valuation reuse,
  the alert, and all dashboard changes.

## File structure

```
Modify:
  src/carbuyer/db/models.py            (+ PrivateListing model)
  src/carbuyer/db/saved_searches.py    (+ adapt_private_listing)
Create:
  alembic/versions/<rev>_private_listings.py
  tests/db/test_private_listing_models.py     (DB: schema/constraints)
  tests/db/test_private_listing_adapter.py     (pure: adapt_private_listing)
```

No new packages. No worker, no scraper, no dashboard.

---

## Task 1: `private_listings` table

**Files:** Modify `src/carbuyer/db/models.py`; Create
`alembic/versions/<rev>_private_listings.py`; Test
`tests/db/test_private_listing_models.py`.

- [ ] **Step 1: Write the failing schema test**

Create `tests/db/test_private_listing_models.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import PrivateListing


def _listing(**ov: object) -> PrivateListing:
    base: dict[str, object] = dict(
        source="kijiji", source_listing_id="L1",
        url="https://kijiji.ca/v/1", canonical_url="https://kijiji.ca/v/1",
    )
    base.update(ov)
    return PrivateListing(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_private_listing_defaults(session: AsyncSession) -> None:
    pl = _listing(make="Ford", model="Mustang", ask_price_cad=Decimal("18000"))
    session.add(pl)
    await session.flush()
    await session.refresh(pl)
    assert pl.id is not None
    assert pl.title_status == "UNKNOWN"          # server_default
    assert pl.photos == []                        # text[] default {}
    assert pl.red_flags == [] and pl.green_flags == []
    assert pl.enrichment_status == "pending" and pl.valuation_status == "pending"
    assert pl.first_seen_at is not None and pl.last_seen_at is not None
    assert pl.removed_at is None and pl.alerted_at is None
    assert pl.created_at is not None


@pytest.mark.asyncio
async def test_private_listing_unique_source(session: AsyncSession) -> None:
    session.add(_listing(source="kijiji", source_listing_id="DUP"))
    await session.flush()
    session.add(_listing(source="kijiji", source_listing_id="DUP"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_private_listing_roundtrips_valuator_and_array_fields(
    session: AsyncSession,
) -> None:
    pl = _listing(
        make="Dodge", model="Viper", year=2005, mileage_km=40000,
        ask_price_cad=Decimal("55000"), photos=["a.jpg", "b.jpg"],
        rarity_score=4.0, expected_value_cad=Decimal("60000"),
        all_in_cost_cad=Decimal("56000"), price_deal_score=0.30,
        pickup_province="AB",
    )
    session.add(pl)
    await session.flush()
    await session.refresh(pl)
    assert pl.photos == ["a.jpg", "b.jpg"]
    assert pl.rarity_score == 4.0
    assert pl.price_deal_score == 0.30
    assert pl.pickup_province == "AB"
```

- [ ] **Step 2: Run it — confirm fail**

Run: `uv run pytest tests/db/test_private_listing_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'PrivateListing'`.

- [ ] **Step 3: Add the `PrivateListing` model**

In `src/carbuyer/db/models.py`, append after the last model (e.g. after
`SavedSearchMatch`). Confirm `BigInteger`, `Boolean`, `DateTime`, `Integer`,
`Numeric`, `String`, `Text`, `UniqueConstraint`, `Index`, `func`, `text`,
`Mapped`, `mapped_column`, `ARRAY`, `JSONB` are already imported (they are, used
by existing models); add any missing to the existing import group.

```python
class PrivateListing(Base, TimestampMixin):
    """A private-party vehicle listing (e.g. Kijiji). Separate from AuctionLot:
    no bids, no buyer premium, no closing time. Enriched + valued + matched by
    the private_sale worker (PR-2), surfaced in the dashboard (PR-3)."""

    __tablename__ = "private_listings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # ── source identity (scraper) ──────────────────────────────────────────
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_listing_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    photos: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'::text[]"), nullable=False,
    )
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    pickup_province: Mapped[str | None] = mapped_column(String(8))
    pickup_city: Mapped[str | None] = mapped_column(String(128))
    ask_price_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # ── vehicle identity (scraper on insert; enricher normalizes) ──────────
    year: Mapped[int | None] = mapped_column(Integer)
    make: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    trim: Mapped[str | None] = mapped_column(String(64))
    vin: Mapped[str | None] = mapped_column(String(32))
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    title_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNKNOWN", server_default="UNKNOWN",
    )

    # ── enricher outputs (LLM) ─────────────────────────────────────────────
    condition_categorical: Mapped[str | None] = mapped_column(String(16))
    red_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    green_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    showstopper_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    rarity_score: Mapped[float | None] = mapped_column()
    summary: Mapped[str | None] = mapped_column(Text)
    desirable_trim_or_spec: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False,
    )
    classic_or_collector: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False,
    )

    # ── valuator outputs ───────────────────────────────────────────────────
    expected_value_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    all_in_cost_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_deal_score: Mapped[float | None] = mapped_column()
    flag_score: Mapped[int | None] = mapped_column(Integer)
    confidence_bucket: Mapped[str | None] = mapped_column(String(16))

    # ── pipeline status + lifecycle ────────────────────────────────────────
    enrichment_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    valuation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_alert_price_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # ── dashboard (user) ───────────────────────────────────────────────────
    user_action: Mapped[UserAction | None] = mapped_column(
        SAEnum(UserAction, native_enum=False, length=16,
               values_callable=lambda x: [e.value for e in x]),
    )
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "source", "source_listing_id",
            name="uq_private_listings_source_listing",
        ),
        Index("ix_private_listings_make_model_year", "make", "model", "year"),
        Index("ix_private_listings_price_deal_score", "price_deal_score"),
        Index("ix_private_listings_user_action", "user_action"),
        Index(
            "ix_private_listings_pending",
            "id",
            postgresql_where=text(
                "enrichment_status = 'pending' OR valuation_status = 'pending'"
            ),
        ),
    )
```

(`Any`, `datetime`, `Decimal`, `SAEnum`, `UserAction`, `TimestampMixin` are
already imported/defined in `models.py`.)

- [ ] **Step 4: Run it — confirm pass**

Run: `uv run pytest tests/db/test_private_listing_models.py -q`
Expected: PASS.

- [ ] **Step 5: Hand-write the migration**

`uv run alembic revision -m "private_listings"`, set `down_revision` to the
current head (`uv run alembic heads`), and write `upgrade`/`downgrade` to mirror
the model exactly — every column with the same type/nullable/server_default, the
unique constraint, the two plain indexes, and the partial pending index:

```python
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    op.create_table(
        "private_listings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_listing_id", sa.String(length=128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("photos", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("pickup_province", sa.String(length=8), nullable=True),
        sa.Column("pickup_city", sa.String(length=128), nullable=True),
        sa.Column("ask_price_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("make", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("trim", sa.String(length=64), nullable=True),
        sa.Column("vin", sa.String(length=32), nullable=True),
        sa.Column("mileage_km", sa.Integer(), nullable=True),
        sa.Column("title_status", sa.String(length=32), server_default="UNKNOWN", nullable=False),
        sa.Column("condition_categorical", sa.String(length=16), nullable=True),
        sa.Column("red_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("green_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("showstopper_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("rarity_score", sa.Double(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("desirable_trim_or_spec", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("classic_or_collector", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expected_value_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("all_in_cost_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("price_deal_score", sa.Double(), nullable=True),
        sa.Column("flag_score", sa.Integer(), nullable=True),
        sa.Column("confidence_bucket", sa.String(length=16), nullable=True),
        sa.Column("enrichment_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("valuation_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alert_price_cad", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("user_action", sa.String(length=16), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_private_listings")),
        sa.UniqueConstraint("source", "source_listing_id", name="uq_private_listings_source_listing"),
    )
    op.create_index("ix_private_listings_make_model_year", "private_listings", ["make", "model", "year"], unique=False)
    op.create_index("ix_private_listings_price_deal_score", "private_listings", ["price_deal_score"], unique=False)
    op.create_index("ix_private_listings_user_action", "private_listings", ["user_action"], unique=False)
    op.create_index(
        "ix_private_listings_pending", "private_listings", ["id"], unique=False,
        postgresql_where=sa.text("enrichment_status = 'pending' OR valuation_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_private_listings_pending", table_name="private_listings")
    op.drop_index("ix_private_listings_user_action", table_name="private_listings")
    op.drop_index("ix_private_listings_price_deal_score", table_name="private_listings")
    op.drop_index("ix_private_listings_make_model_year", table_name="private_listings")
    op.drop_table("private_listings")
```

Note: `user_action` is stored as `String(16)` in the migration (the model's
`SAEnum(native_enum=False)` is a varchar, exactly like `AuctionLot.user_action`).
All indexes live in the model's `__table_args__` — no `index=True` on individual
columns — so the migration's four `create_index` calls (make_model_year,
price_deal_score, user_action, pending) are the complete, exact mirror of the
model. (`first_seen_at`/`last_seen_at` use `server_default=now()`, matching the
model.)

- [ ] **Step 6: Re-run + confirm single head**

Run: `uv run pytest tests/db/test_private_listing_models.py -q` → PASS;
`uv run alembic heads` → exactly one head (the new revision).

- [ ] **Step 7: Commit**

```bash
git add src/carbuyer/db/models.py tests/db/test_private_listing_models.py alembic/versions/
git commit -m "feat(db): add private_listings table"
```

---

## Task 2: `adapt_private_listing` matching adapter

**Files:** Modify `src/carbuyer/db/saved_searches.py`; Test
`tests/db/test_private_listing_adapter.py`.

- [ ] **Step 1: Write the failing adapter test**

Create `tests/db/test_private_listing_adapter.py`:

```python
from __future__ import annotations

from decimal import Decimal

from carbuyer.db.models import PrivateListing
from carbuyer.db.saved_searches import MatchableListing, adapt_private_listing, match_listing
from carbuyer.db.models import SavedSearch


def test_adapt_maps_fields_and_kind() -> None:
    pl = PrivateListing(
        id=7, source="kijiji", source_listing_id="L1",
        url="u", canonical_url="u",
        make="Ford", model="Mustang", year=1968, trim="Fastback",
        mileage_km=90_000, title_status="NORMAL", condition_categorical="good",
        pickup_province="AB", all_in_cost_cad=Decimal("25000.50"), rarity_score=2.1,
    )
    m = adapt_private_listing(pl)
    assert isinstance(m, MatchableListing)
    assert m.source_kind == "private_listing"
    assert m.source_id == 7
    assert m.make == "Ford" and m.model == "Mustang" and m.year == 1968
    assert m.province == "AB"
    assert m.all_in_cost_cad == 25_001        # ceil, like adapt_auction_lot
    assert m.rarity_score == 2.1


def test_adapt_none_all_in_cost() -> None:
    pl = PrivateListing(id=8, source="kijiji", source_listing_id="L2",
                        url="u", canonical_url="u", make="Ford")
    assert adapt_private_listing(pl).all_in_cost_cad is None


def test_adapted_listing_matches_a_saved_search() -> None:
    pl = PrivateListing(id=9, source="kijiji", source_listing_id="L3",
                        url="u", canonical_url="u", make="Ford", model="Mustang",
                        year=1968, pickup_province="AB")
    s = SavedSearch(name="stangs", make="Ford", model="Mustang", province=["AB"])
    assert match_listing(adapt_private_listing(pl), s) is True
    s_bc = SavedSearch(name="bc", make="Ford", province=["BC"])
    assert match_listing(adapt_private_listing(pl), s_bc) is False
```

- [ ] **Step 2: Run it — confirm fail**

Run: `uv run pytest tests/db/test_private_listing_adapter.py -q`
Expected: FAIL — `ImportError: cannot import name 'adapt_private_listing'`.

- [ ] **Step 3: Implement the adapter**

In `src/carbuyer/db/saved_searches.py`, add `PrivateListing` to the model
import and add the adapter (mirrors `adapt_auction_lot`, including the
`math.ceil` cost-cap rounding; `math` is already imported):

```python
def adapt_private_listing(listing: PrivateListing) -> MatchableListing:
    all_in = listing.all_in_cost_cad
    return MatchableListing(
        source_kind="private_listing",
        source_id=listing.id,
        make=listing.make,
        model=listing.model,
        year=listing.year,
        trim=listing.trim,
        mileage_km=listing.mileage_km,
        title_status=listing.title_status,
        condition_categorical=listing.condition_categorical,
        province=listing.pickup_province,
        all_in_cost_cad=math.ceil(all_in) if all_in is not None else None,
        rarity_score=listing.rarity_score,
    )
```

Update the model import line from
`from carbuyer.db.models import Auction, AuctionLot, SavedSearch` to also import
`PrivateListing`.

- [ ] **Step 4: Run it — confirm pass**

Run: `uv run pytest tests/db/test_private_listing_adapter.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/db/saved_searches.py tests/db/test_private_listing_adapter.py
git commit -m "feat(db): adapt_private_listing -> MatchableListing (source_kind=private_listing)"
```

---

## Task 3: Lint, type-check, full-suite gate

**Files:** none (verification; fix in place).

- [ ] **Step 1: Ruff** — `uv run ruff check src/carbuyer/db/models.py src/carbuyer/db/saved_searches.py tests/db/test_private_listing_models.py tests/db/test_private_listing_adapter.py --output-format=concise` → no new errors. `# type: ignore[arg-type]` on the `**base` construction in the schema test matches the codebase convention.
- [ ] **Step 2: Pyright** — `uv run pyright src/carbuyer/db/saved_searches.py` → no new errors. (`models.py` has ~8 PRE-EXISTING `SAEnum(values_callable=lambda...)` errors; the new `PrivateListing.user_action` uses the identical pattern, so it will add the SAME pre-existing-style error there — that's consistent with `AuctionLot`/`LotActionHistory`; confirm you add no OTHER new errors.)
- [ ] **Step 3: Full suite** — `uv run pytest -q` → all green (new tests:
  `test_private_listing_models.py`, `test_private_listing_adapter.py`). Note: a
  pre-existing wall-clock-flaky test (`test_today::test_closing_buckets_bin_by_time`)
  may intermittently fail unrelated to this PR.
- [ ] **Step 4: Commit any fixes** — `git commit -m "chore(db): satisfy ruff/pyright for private_listings"`.

---

## Self-review (controller checklist)

- **Spec coverage:** this PR is the spec's "Foundation" minus `RawPrivateListing`
  (deferred to PR-2, where the scraper produces + tests it). Delivers the
  `private_listings` table (§Components 1) and `adapt_private_listing`
  (§Components 3 matcher row) — both independently testable. The enrich/value/
  alert/worker/dashboard sections are explicitly later PRs.
- **Type consistency:** `PrivateListing` field names ↔ the migration columns ↔
  `adapt_private_listing` reads ↔ `MatchableListing` fields (PR-2) all agree.
  `user_action` is `SAEnum(native_enum=False)` ↔ `String(16)` in the migration
  (matches `AuctionLot`). `all_in_cost_cad` Decimal → `math.ceil` int (matches
  `adapt_auction_lot`).
- **Placeholder scan:** none. All indexes live in `__table_args__` (no
  `index=True` on individual columns), so model and migration mirror exactly —
  four indexes: make_model_year, price_deal_score, user_action, pending.
- **down_revision:** the one value to fill from `alembic heads` (flagged in
  Task 1 Step 5).
```
