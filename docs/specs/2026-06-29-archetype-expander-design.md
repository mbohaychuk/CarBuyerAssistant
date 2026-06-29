# Archetype Expander — Design & Spec

**Date:** 2026-06-29
**Status:** Approved — ready for implementation plan
**Phase:** Phase 2 (want-list pivot), design-doc §5e / §4 archetype fan-out
**Depends on:** want-list spine (Phase 0), `vehicle_offer` split (Phase 1), the WG5 flipper teardown (`89e5289`)

---

## TL;DR

A fuzzy want archetype ("cheap reliable 4Runner-platform offroad base") is expanded by
`gpt-5-nano` into a set of concrete `(make, model, year-range, trim)` rows, each snapped to
the NHTSA-canonical model spelling, which the owner reviews/edits on the dashboard before it
becomes one active want with a shared price/condition rule. The differentiator: one fuzzy
want fans out to multiple platform-sibling models (GX470 + 4Runner + GX460…) that no
mainstream tool groups.

The only genuinely new logic is the expander (LLM call + per-row vPIC normalization). Storage
extends `WantCriteria` additively with a `model_specs` list, so it rides the existing
matcher / repo / backfill / deal-score machinery with **no migration**.

---

## 1. Goal & non-goals

**Goal:** let the owner declare a want by intent ("reliable body-on-frame offroad under $18k")
instead of enumerating every make/model/year by hand, with a human-in-the-loop confirm step
so the LLM never silently creates a matching rule.

**Non-goals (YAGNI):**
- No Discord surface — dashboard only (owner's choice).
- No per-spec transmission/drivetrain — those stay shared across the want's models.
- No expansion versioning / history — re-expansion is "edit the text, Expand again".
- No automatic re-expansion on a schedule — expansion happens only on the explicit button press.

**Success criteria:** typing an archetype + Expand yields a sensible, vPIC-normalized model
set within one LLM round-trip; the owner can drop/edit rows and Save; the saved want then
matches lots exactly as a hand-built multi-model want would, and `backfill_want` surfaces
existing matches immediately.

## 2. Reuse map

Order of preference applied: reuse as-is → extend → new.

**Reuse as-is:**
- `normalize/vpic.py` `canonical_model(make, model, year, *, client)` — snaps an LLM model
  string to the NHTSA spelling; best-effort (returns input on failure/no-match).
- `wants/repo.py` `create_want` / `list_wants` / `update_want` / `delete_want`.
- `wants/service.py` `backfill_want` (seeds matches from already-valued open lots/listings)
  and `evaluate_lot_against_wants` (forward path) — both already iterate `WantCriteria`.
- `wants/deal.py` `score_want_deal` — unchanged (reads `expected_value`/`value_mid`/`comp_count`).
- `llm/openai_provider.py` `_parse_to` — the single structured-output chokepoint
  (`chat.completions.parse`, usage logging, reasoning-model param dispatch).
- `flags/taxonomy.py` `DESIRABLE_TRIMS` — seeds the expansion prompt with known
  platform/desirability relationships (GX470/GX460, 4Runner/Tacoma TRD, Wrangler Rubicon).

**Extend:**
- `wants/criteria.py` `WantCriteria` — add `archetype_text` + `model_specs` (see §4).
- `wants/matcher.py` `matches` + `could_match_any_want` — add the `model_specs` OR-branch (§5).
- `llm/base.py` — add an `ArchetypeProvider` role ABC, symmetric with `DescribeProvider` /
  `VisionProvider`.
- `llm/openai_provider.py` — implement `expand_archetype` via `_parse_to`.
- `llm/prompts.py` + `llm/schemas.py` — the archetype prompt + `ArchetypeExpansion` schema.
- `apps/dashboard/routers/wants.py` + templates — the Expand endpoint + editable-table partial.

**Build new:**
- `wants/archetype.py` — `expand_archetype(text, *, provider, client) -> list[ModelSpec]`:
  call the provider, normalize each row's model via vPIC. The orchestration seam; pure of the
  dashboard and of the OpenAI SDK (both injected) so it unit-tests without network or HTTP.

## 3. End-to-end flow

```
Dashboard /wants
  ┌─ archetype text  ──[Expand]──▶ POST /wants/expand
  │                                   └─ expand_archetype(text, provider, http)
  │                                        ├─ provider.expand_archetype(text) → ArchetypeExpansion (gpt-5-nano)
  │                                        └─ per row: vpic.canonical_model(make, model, year_mid) → snap spelling
  │                                   ◀── editable rows (HTMX partial)
  ├─ review table: ✓/✗ rows, edit year/trim per row
  ├─ shared rule: name, price ceiling, provinces, mileage, condition, transmission/drivetrain, hide_showstoppers
  └─ [Save] ──▶ POST /wants
                  └─ WantCriteria(model_specs=[checked rows], archetype_text=text, <shared rule>)
                     ├─ repo.create_want
                     └─ service.backfill_want  (existing matches shown)
```

Expansion produces **candidates only** — nothing is persisted until Save. Re-expansion just
re-runs `POST /wants/expand` with edited text and replaces the candidate table.

## 4. Storage — `WantCriteria` extension

`Search.config` JSONB stays the home. Two additive fields:

```python
class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    make: str
    model: str
    year_min: int | None = None
    year_max: int | None = None
    trims: list[str] = []

class WantCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # NEW
    archetype_text: str | None = None      # original fuzzy text; display + re-expand seed
    model_specs: list[ModelSpec] = []      # the expanded concrete set
    # existing flat fields stay = the want's SHARED rule
    makes: list[str] = []
    models: list[str] = []
    trims: list[str] = []
    transmissions: list[Transmission] = []
    drivetrains: list[Drivetrain] = []
    year_min: int | None = None
    year_max: int | None = None
    price_ceiling_cad: int | None = None
    max_mileage_km: int | None = None
    provinces: list[str] = []
    condition_min: Condition | None = None
    hide_showstoppers: bool = True
```

**Backward compatibility:** both new fields default-empty, so every existing flat want still
validates under `extra="forbid"` — **no migration, no `schema_version` bump** (the §8
want-list-pivot risk note is satisfied: adding fields with defaults is the backward-safe case).

**Two shapes, one type:**
- *Manual want* (today's `/want add`, dashboard manual entry): flat `makes`/`models`, `model_specs=[]`.
- *Archetype want* (this feature): `model_specs` populated, flat `makes`/`models` empty,
  `archetype_text` set.

`model_specs` and flat `makes`/`models` are **not** meant to be combined on one want; the
matcher (§5) treats a non-empty `model_specs` as the identity source and ignores the flat
make/model on that want.

A `ModelSpec` validator enforces `year_min <= year_max` when both present (mirrors the
existing `WantCriteria._year_range_ordered`).

## 5. Matching

`matches(lot, criteria, …)` keeps its shared filters (price, province, mileage, condition,
transmission, drivetrain, hide_showstoppers — all AND'd, all lenient-on-unknown as today).
The identity check changes:

```
identity_ok =
    if criteria.model_specs:  ANY spec matches:
        _in_set(lot.make,  [spec.make],  lenient_unknown=False)
        and _in_set(lot.model, [spec.model], lenient_unknown=False)
        and _year_in_range(lot.year, spec.year_min, spec.year_max)
        and _in_set(lot.trim, spec.trims, lenient_unknown=True)
    else:                     existing flat path:
        _in_set(make, criteria.makes, …) and _in_set(model, criteria.models, …)
        and _year_in_range(lot.year, criteria.year_min, criteria.year_max)
        and _in_set(trim, criteria.trims, …)
```

So a spec carries its own year-range + trims; the want's flat `year_min/max` and `trims`
apply only to the legacy flat path. Reuses the existing `_in_set` / `_year_in_range` helpers
verbatim — the spec branch is a small OR-loop, not a new matching algorithm.

**Coarse gate (`could_match_any_want`, WG1):** the cheap title-substring pre-match gains the
same OR-over-specs: a raw offer plausibly matches if **any** spec's make+model terms appear in
the title and the year (if known) fits that spec's range. Empty-criteria semantics unchanged.

**No separate SQL builder.** Post-WG5 the dashboard surfaces matches from the `want_matches`
ledger (count group-by on the list page, `WantMatch ⋈ AuctionLot` on the detail page), not a
live make/model query — `feed.py` is gone. Matches are produced by the Python `matches()`
predicate via `service.backfill_want` (new want) and `service.evaluate_lot_against_wants`
(forward valuator path). So extending `matches()` + `could_match_any_want()` is the **whole**
matching change — both `backfill_want` and the forward path inherit `model_specs` support with
no further edits.

## 6. LLM expansion

**Schema** (`llm/schemas.py`):
```python
class ExpandedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    make: str
    model: str
    year_min: int | None
    year_max: int | None
    trims: list[str]
    reason: str          # one line: why this model fits the archetype (shown in the table)

class ArchetypeExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[ExpandedModel]
```

**Provider role** (`llm/base.py`): `ArchetypeProvider(_AsyncCM, ABC)` with
`async def expand_archetype(self, text: str) -> ArchetypeExpansion`. `OpenAIProvider`
implements it through `_parse_to(response_format=ArchetypeExpansion, …, kind="archetype")`.

**Prompt** (`llm/prompts.py`): system prompt instructs the model to expand a buyer's fuzzy
archetype into concrete **North-American-market** models with realistic year ranges and trim
hints, using enthusiast/platform knowledge (e.g. "GX470 ≈ J120 4Runner platform", "manual
Xterra"), and to stay tight (return the genuinely-fitting siblings, not every SUV). Seeded
with a compact rendering of `taxonomy.DESIRABLE_TRIMS` as worked examples. Output is the
`ArchetypeExpansion` schema.

**Normalization:** for each `ExpandedModel`, `expand_archetype` calls
`vpic.canonical_model(make, model, year)` (year = `year_min` or the midpoint) to snap the
model spelling before returning the `ModelSpec`. Best-effort: vPIC failure keeps the LLM
spelling.

## 7. Dashboard

- `routers/wants.py` gains `POST /wants/expand` (form: `archetype_text`) → calls
  `expand_archetype` with a request-scoped `OpenAIProvider` + `httpx.AsyncClient` → renders an
  editable-rows partial (one row per `ModelSpec`: checkbox, make, model, year_min, year_max,
  trims, reason).
- The existing want-create `POST /wants` handler is extended: alongside the shared-rule form
  fields it accepts the checked `model_specs` rows (repeated form fields per row) and builds a
  `WantCriteria` with `model_specs` + `archetype_text`. `from_inputs` (CSV → flat lists) stays
  the manual path; archetype saves construct `model_specs` directly.
- Templates: an `expand` form + a `model_specs` table partial, following the existing HTMX
  action pattern on the `/wants` page. The want detail/list view shows `archetype_text` when
  present ("expanded from: …").
- Provider lifecycle: instantiate per request inside the handler and close via `async with`
  (the provider is an async CM); the dashboard is low-traffic single-user, so no pooled
  provider is warranted.

## 8. Error handling

- **vPIC** per row: best-effort, keep LLM spelling on any failure (already the function's
  contract).
- **LLM**: `expand_archetype` lets `APIError`/`RateLimitError` surface; the dashboard handler
  catches and renders "Couldn't expand the archetype right now — add models manually below."
  The table still supports manual row entry, so a failed expansion never blocks want creation.
- **Empty expansion** (`models == []`): same manual-fallback message.
- **Validation** on Save: a `ModelSpec` needs non-empty make + model; `year_min <= year_max`;
  errors surface via the existing `first_error` summary.

## 9. Testing (TDD)

- `expand_archetype`: stub `ArchetypeProvider` + stub `httpx` (or stub `canonical_model`) →
  asserts rows are returned, model spelling is normalized (vPIC snap applied), year/trim
  parsed, and a provider failure / empty result yields an empty list (manual fallback path).
- `matcher`: a lot matches the spec whose year-range it falls in and **not** a sibling spec
  with a different range; trims honored per spec; the **flat path is unchanged** (regression
  test pinning a legacy flat want); the coarse gate ORs over specs.
- `criteria` round-trip: `model_specs` ↔ JSONB; a pre-existing flat config (no `model_specs`)
  still `model_validate`s (backward-compat pin); `ModelSpec` year-order validator.
- `dashboard`: `POST /wants/expand` returns the rows partial; Save builds a want with the
  checked specs and triggers `backfill_want`.

## 10. Build sequence (for the plan)

1. `WantCriteria` + `ModelSpec` (schema + round-trip + backward-compat tests).
2. `matcher` model_specs branch in `matches` + `could_match_any_want` (TDD on the OR logic;
   regression-pin the flat path).
3. `ArchetypeExpansion` schema + `ArchetypeProvider` role + `OpenAIProvider.expand_archetype`
   + prompt (seeded from taxonomy).
4. `wants/archetype.py` `expand_archetype` (LLM + vPIC; stubbed-provider tests).
5. Dashboard expand endpoint + editable-table partial + extended Save (build `model_specs` +
   `archetype_text`) + want-detail `archetype_text` display.

Each step compiles + tests green before the next.

## 11. Risks

- **Comp scarcity for cross-platform archetypes** (design-doc §8): a model_specs want spanning
  GX470 + 4Runner has comps keyed per concrete model — comps are still per-`(make, model)`, so
  this is no worse than separate wants; the deal score is per-lot. No new comp work here.
- **LLM hallucinating non-NA or wrong-year models** — mitigated by the human confirm gate
  (nothing is saved unreviewed) + vPIC normalization snapping/validating the model spelling.
- **`config` schema growth** — handled: additive defaulted fields keep `extra="forbid"`
  backward-safe; a future *removal/rename* of a criterion still needs the `schema_version`
  migration the §8 note describes, but this change does not.
