# Enrichment Data-Quality Fixes — Design

**Goal:** Make the lot-detail page faithfully surface what the enrichment
pipeline already knows, and give the LLM a channel to express judgement the
fixed flag taxonomy cannot capture.

**Status:** approved 2026-05-19. Branch `enrichment-data-quality`, single PR.

---

## Motivation

A production lot showed condition "decent" while its description listed serious
damage; engine-light lots were not flagged; description and mileage provenance
were not shown. Investigation found four independent root causes plus a
template bug. The McDougall parser bug (empty descriptions) was already fixed
in PR #14 — this workstream addresses the rest.

## Changes

### 1. Flag-evidence template bug

`lot_detail.html` lines 57/63/69 render `{{ f.description }}` and
`_macros.html:70` (`flag_chip`) reads `flag.description`. Flag dicts only carry
keys `flag`, `evidence`, `weight` (`FlagInstance`) / `flag`, `evidence`
(`ShowstopperInstance`) — there is no `description` key. Every Concern,
Strength and Showstopper therefore renders its chip with **no evidence text**.

Fix: rename `description` → `evidence` at all four sites. `flag_chip` is also
used by `lot_card.html`; the change is a safe no-op there (the title attribute
was always empty) and enables the tooltip everywhere.

### 2. RC#2 — show the listing description

`AuctionLot.description` (`models.py:147`, `Text`, nullable) is populated and
fed to the LLM but never rendered. Add a "Listing description" section to
`lot_detail.html` after the Summary block. Jinja autoescapes the text; source
text contains newlines, so the container needs `white-space: pre-wrap`.

### 3. RC#3 — sparse-listing condition is indistinguishable from a real rating

When `condition_confidence < 0.5` the enricher forces
`condition_categorical = "decent"` and sets
`condition_inferred_from_sparse_listing = True`. The Specs list renders a bare
"decent" identical to a confident rating. Fix: when
`condition_inferred_from_sparse_listing` is true, render the condition with a
muted qualifier, e.g. `decent — inferred from a sparse listing`.

### 4. RC#4 — mileage provenance

No column records whether mileage is verified. Sources state it inline as plain
text (McDougall: "Mileage (Showing Unverified): …"). The LLM already reads the
description, so provenance is extracted there — consistent with how every other
field is normalized.

- New field `NormalizedVehicle.mileage_is_verified: bool | None`
  (`true` = explicitly verified, `false` = explicitly unverified/TMU/"showing",
  `null` = listing says nothing).
- New column `AuctionLot.mileage_is_verified: Boolean`, nullable.
- `lot_detail.html` line 17 shows a muted `(unverified)` marker beside the km
  when the value is `false`.

### 5. RC#1 — advisory LLM concerns

The system prompt says "Use ONLY the flag taxonomies below. Do not invent new
flags." That is correct for `red_flags` (their keys drive `flag_score` via the
taxonomy weight lookup) but it leaves the LLM no way to express the non-obvious,
correlated judgement that is the whole reason to use an LLM over keyword search.

Add a free-text **advisory** channel:

- New schema model `Concern { text: str, severity: "minor"|"moderate"|"serious" }`.
- New field `EnrichmentOutput.concerns: list[Concern]`.
- New column `AuctionLot.llm_concerns: JSONB`, non-null, default `[]`.
- System prompt gains a rule inviting free-form concerns that the taxonomy does
  not cover.
- Rendered in its own row of the "Condition signals" section.
- **`flag_score` is NOT touched.** `scoring/score.py:flag_score` reads only the
  taxonomy red/green flags and `description_quality`. Advisory concerns are
  visible to the user but never silently move rankings — this is the "advisory,
  not scored" decision.

## Re-enrichment / backfill

Existing rows have no `concerns` and no `mileage_is_verified`. Bump
`settings.enrichment_version` `"v1"` → `"v2"` (`shared/config.py`).

The re-pend mechanism the `config.py:50` comment describes turned out never to
have been implemented — `enrichment_version` was a dead field. This workstream
implements it: on enricher startup (`_catchup_sweep`, before orphan recovery)
the worker runs

```sql
UPDATE auction_lots SET enrichment_status = 'pending'
WHERE enrichment_status = 'done'
  AND enrichment_version IS DISTINCT FROM :current_version
```

Scoping to `enrichment_status = 'done'` is deliberate: FAILED lots never get
`enrichment_version` stamped (it is written only on the success path), so an
unscoped re-pend would retry them on every startup forever. The operation is
idempotent — a re-enriched lot is stamped with the current version and is not
matched again. The first enricher run after deploy thus backfills both new
fields across the existing corpus.

## Schema summary

```python
# llm/schemas.py
ConcernSeverity = Literal["minor", "moderate", "serious"]

class Concern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    severity: ConcernSeverity

# NormalizedVehicle gains:   mileage_is_verified: bool | None
# EnrichmentOutput gains:    concerns: list[Concern]
```

```
# auction_lots — new columns
llm_concerns          JSONB    NOT NULL  DEFAULT '[]'::jsonb
mileage_is_verified   BOOLEAN  NULL
```

One Alembic revision adds both columns; `down_revision = a9cef3ed161c`.

## Out of scope

- Dashboard PRs 4–6 (watchlist kanban, place-bid modal, action history) — a
  separate spec/branch.
- Expanding the flag taxonomy — explicitly rejected; the advisory `concerns`
  channel is the chosen alternative.
- Re-scoring based on concerns — advisory only, by decision.

## Testing

- Schema: `concerns` / `mileage_is_verified` validate; `extra="forbid"` holds.
- Migration: upgrade adds columns with correct defaults; downgrade drops them;
  round-trip clean.
- Enricher: `_apply_to_lot` writes `llm_concerns` and `mileage_is_verified`.
- Templates: evidence text renders; description renders; sparse-listing
  qualifier renders; concerns section renders; unverified marker renders.
- Full suite green; no `flag_score` test regressions.
