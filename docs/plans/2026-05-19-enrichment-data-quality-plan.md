# Enrichment Data-Quality Fixes — Implementation Plan

> **For agentic workers:** executed via subagent-driven-development — fresh
> subagent per task, two-stage review (spec compliance, then code quality)
> after each. Steps use `- [ ]` checkboxes.

**Goal:** Surface enrichment data the pipeline already produces, and add an
advisory free-text `concerns` channel for LLM judgement.

**Spec:** `docs/specs/2026-05-19-enrichment-data-quality-design.md`

**Tech stack:** Python 3.14, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL,
Jinja2 templates, pytest-asyncio. Test runner: `uv run pytest -q`.

**Branch:** `enrichment-data-quality` (off `main`). TDD throughout — failing
test first. Commit after each task.

---

## Task 1 — Template-only fixes (flag-evidence bug, RC#2, RC#3)

No schema change, no migration. All edits in
`src/carbuyer/apps/dashboard/templates/`.

**Files:**
- Modify: `templates/pages/lot_detail.html`
- Modify: `templates/_macros.html`
- Modify: the dashboard stylesheet (find the CSS source the Makefile `css`
  target compiles; add minimal rules there, not the compiled output)
- Test: `tests/apps/dashboard/` — extend the existing lot-detail view test
  module (locate via `grep -rl lot_detail tests/`)

**Steps:**

- [ ] **1a. Flag-evidence bug.** In `lot_detail.html` replace `{{ f.description }}`
  with `{{ f.evidence }}` at the three flag rows (showstopper, red, green). In
  `_macros.html` `flag_chip`, replace both `flag.description` references with
  `flag.evidence`. Verify `lot_card.html` still renders (it uses `flag_chip`;
  flag dicts never had a `description` key, so this only enables the tooltip).

- [ ] **1b. RC#2 — listing description.** After the Summary `{% if %}` block in
  `lot_detail.html`, add:
  ```jinja
  {% if lot.description %}
    <section class="lot-detail__section">
      <h2>Listing description</h2>
      <p class="lot-detail__description">{{ lot.description }}</p>
    </section>
  {% endif %}
  ```
  Add a CSS rule for `.lot-detail__description { white-space: pre-wrap; }` so
  source newlines render. (Jinja autoescaping stays on — do not mark safe.)

- [ ] **1c. RC#3 — sparse-listing condition.** Replace the Condition `<dd>` in
  the Specs list so that when `lot.condition_inferred_from_sparse_listing` is
  true it renders `{{ lot.condition_categorical }}` followed by a muted
  `<span class="t-meta">— inferred from a sparse listing</span>`; otherwise the
  bare value as today.

- [ ] **1d. Tests.** Write/extend view tests: a lot with flags renders the
  evidence text; a lot with a description renders the "Listing description"
  section; a lot with `condition_inferred_from_sparse_listing=True` renders the
  qualifier and one with it `False` does not. Run `uv run pytest -q` — all green.

- [ ] **1e. Commit.** `git commit` — message e.g.
  `lot detail: render flag evidence, listing description, sparse-condition note`.

---

## Task 2 — Schema models + Alembic migration

Adds the new schema fields and DB columns. No behaviour wiring yet.

**Files:**
- Modify: `src/carbuyer/llm/schemas.py`
- Modify: `src/carbuyer/db/models.py`
- Create: `alembic/versions/<rev>_enrichment_concerns_mileage_provenance.py`
- Test: schema test module + migration test (mirror the four-state migration
  test — locate via `grep -rl alembic tests/`)

**Steps:**

- [ ] **2a. Failing schema test.** Assert `Concern` validates a
  `{text, severity}` dict, rejects an unknown `severity` and rejects extra
  keys; assert `EnrichmentOutput` requires `concerns` and `NormalizedVehicle`
  requires `mileage_is_verified`. Run — fails.

- [ ] **2b. Schema models.** In `schemas.py` add near `FlagInstance`:
  ```python
  ConcernSeverity = Literal["minor", "moderate", "serious"]

  class Concern(BaseModel):
      model_config = ConfigDict(extra="forbid")
      text: str
      severity: ConcernSeverity
  ```
  Add `mileage_is_verified: bool | None` to `NormalizedVehicle` and
  `concerns: list[Concern]` to `EnrichmentOutput`. Run 2a — passes.

- [ ] **2c. Model columns.** In `models.py` `AuctionLot`, add in the
  description-enricher–owned block:
  ```python
  llm_concerns: Mapped[list] = mapped_column(
      JSONB, nullable=False, server_default=text("'[]'::jsonb"),
  )
  mileage_is_verified: Mapped[bool | None] = mapped_column(Boolean)
  ```
  Match the exact import/style of the existing `red_flags` JSONB column and the
  `mileage_km` column. Place `mileage_is_verified` next to `mileage_km`.

- [ ] **2d. Migration.** `uv run alembic revision -m "enrichment concerns + mileage provenance"`,
  then fill in (`down_revision = "a9cef3ed161c"`):
  ```python
  def upgrade() -> None:
      op.add_column("auction_lots", sa.Column(
          "llm_concerns", postgresql.JSONB(astext_type=sa.Text()),
          nullable=False, server_default=sa.text("'[]'::jsonb")))
      op.add_column("auction_lots", sa.Column(
          "mileage_is_verified", sa.Boolean(), nullable=True))

  def downgrade() -> None:
      op.drop_column("auction_lots", "mileage_is_verified")
      op.drop_column("auction_lots", "llm_concerns")
  ```

- [ ] **2e. Migration test.** Mirror the four-state migration test: upgrade →
  both columns exist, `llm_concerns` defaults to `[]` for a pre-existing row,
  `mileage_is_verified` is NULL; downgrade → both dropped; round-trip clean.

- [ ] **2f. Run + commit.** `uv run pytest -q` green. Commit:
  `schema: add llm_concerns + mileage_is_verified columns`.

---

## Task 3 — Enricher write-back + prompt (depends on Task 2)

**Files:**
- Modify: `src/carbuyer/apps/enricher/enricher.py` (`_apply_to_lot`)
- Modify: `src/carbuyer/llm/prompts.py` (`description_system_prompt`)
- Modify: `src/carbuyer/shared/config.py` (`enrichment_version`)
- Test: `tests/apps/` enricher test module

**Steps:**

- [ ] **3a. Failing enricher test.** Stub the LLM provider to return an
  `EnrichmentOutput` with two `concerns` and `normalized_vehicle.mileage_is_verified=False`;
  assert `_apply_to_lot` writes `lot.llm_concerns` (list of `{text, severity}`
  dicts) and `lot.mileage_is_verified = False`.

- [ ] **3b. Write-back.** In `_apply_to_lot` add:
  ```python
  lot.llm_concerns = [c.model_dump() for c in out.concerns]
  lot.mileage_is_verified = nv.mileage_is_verified
  ```
  (`nv` is the existing `normalized_vehicle` local; place near the existing
  `red_flags` / `mileage_km` assignments.)

- [ ] **3c. Prompt.** In `description_system_prompt` GENERAL RULES add two
  bullets. For `concerns`: instruct the model to surface up to ~5 free-form
  judgements the fixed taxonomy does NOT capture — non-obvious risks,
  correlations, inferences from the description (give the worked example
  "blue smoke on cold start + 240k km → likely worn valve seals, budget a
  top-end job"); each needs a `severity` of minor/moderate/serious; empty list
  only when nothing warrants comment; do not restate taxonomy flags. For
  `normalized_vehicle.mileage_is_verified`: `true` when the listing explicitly
  states the odometer is verified/actual, `false` when it states
  unverified/TMU/"showing"/exempt, `null` when the listing is silent. Keep the
  existing "Use ONLY the flag taxonomies … Do not invent new flags" line — it
  scopes to flags; `concerns` is the sanctioned escape hatch.

- [ ] **3d. Re-enrichment.** Bump `enrichment_version` `"v1"` → `"v2"` in
  `config.py`. Confirm (grep) the re-pend mechanism the `config.py:50` comment
  describes so the backfill is real; note the finding in the commit message.

- [ ] **3e. Run + commit.** `uv run pytest -q` green. Commit:
  `enricher: persist llm_concerns + mileage provenance, invite free-form concerns`.

---

## Task 4 — Template: concerns section + mileage marker (depends on Task 2)

**Files:**
- Modify: `templates/pages/lot_detail.html`
- Modify: the dashboard stylesheet
- Test: lot-detail view test module

**Steps:**

- [ ] **4a. Failing view test.** A lot with `llm_concerns` renders each
  concern's text and a severity indicator; a lot with `mileage_is_verified=False`
  renders an `(unverified)` marker beside the km; `True`/`None` do not.

- [ ] **4b. Concerns row.** In the "Condition signals" `<section>` extend the
  guard at the top to `{% if lot.showstopper_flags or lot.red_flags or
  lot.green_flags or lot.llm_concerns %}` and add a flag row:
  ```jinja
  {% if lot.llm_concerns %}
    <div class="lot-detail__flagrow">
      <h3>Analyst notes</h3>
      <ul>{% for c in lot.llm_concerns %}
        <li><span class="concern concern--{{ c.severity }}">{{ c.severity }}</span>
            <span class="t-meta">{{ c.text }}</span></li>
      {% endfor %}</ul>
    </div>
  {% endif %}
  ```

- [ ] **4c. Mileage marker.** On line 17, after the km value, add
  `{% if lot.mileage_is_verified == False %} <span class="t-meta">(unverified)</span>{% endif %}`
  (explicit `== False` — `None` must not trigger it).

- [ ] **4d. CSS.** Add minimal rules for `.concern` and the three
  `.concern--minor/moderate/serious` severity variants (muted → emphatic).

- [ ] **4e. Run + commit.** `uv run pytest -q` green. Commit:
  `lot detail: render analyst concerns + unverified-mileage marker`.

---

## Task 5 — `enrichment_version` re-pend mechanism

The `config.py:50` comment describes a re-pend keyed on `enrichment_version`
that was never implemented — so the v1→v2 bump in Task 3 does not by itself
re-enrich the existing corpus. This task implements it, so the new
`llm_concerns` / `mileage_is_verified` fields backfill on the first enricher
run after deploy.

**Files:**
- Modify: `src/carbuyer/apps/enricher/enricher.py` (`_catchup_sweep`, + a new
  re-pend helper) — or place the helper in `src/carbuyer/db/queue.py` next to
  `recover_orphans` if that reads more naturally; implementer's judgement.
- Test: the enricher / queue test module that already covers
  `recover_orphans` / `select_pending_ids`.

**Steps:**

- [ ] **5a. Failing test.** Seed four lots: (i) `enrichment_status=DONE`,
  `enrichment_version="v1"`; (ii) `DONE`, `enrichment_version="v2"` (current);
  (iii) `FAILED`, `enrichment_version=NULL`; (iv) `PENDING`, never enriched.
  Call the new re-pend function with current version `"v2"`. Assert: (i) →
  `PENDING`; (ii), (iii), (iv) unchanged. Assert the returned count is 1.

- [ ] **5b. Re-pend helper.** Add an async function that issues:
  ```python
  update(AuctionLot)
      .where(
          AuctionLot.enrichment_status == EnrichmentStatus.DONE,
          AuctionLot.enrichment_version.is_distinct_from(
              settings.enrichment_version),
      )
      .values(enrichment_status=EnrichmentStatus.PENDING)
  ```
  Return the affected row count (`result.rowcount`). Scope to `DONE` is
  mandatory — FAILED lots have no `enrichment_version`, an unscoped re-pend
  would retry them every startup.

- [ ] **5c. Wire into startup.** Call the helper at the START of
  `_catchup_sweep`, before `recover_orphans`, inside a session/transaction.
  Log the count at `info` (or `warning` when > 0) so an operator sees the
  backfill happen. The existing `select_pending_ids` drain loop then picks the
  re-pended rows up — no extra NOTIFY needed.

- [ ] **5d. Run + commit.** `uv run pytest -q` green. Commit:
  `enricher: re-pend stale-enrichment_version lots at startup`.

---

## Final review

After Task 5: full-branch code review, `uv run pytest -q` green, manual
spot-check of a lot-detail page in the browser if feasible, then open the PR.
