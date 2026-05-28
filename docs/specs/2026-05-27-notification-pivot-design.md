# Notification Pivot — Design

**Goal:** Move the alerting model from a per-lot real-time trickle to an
auction-event-centric system: tiered going-cheap alerts, a user-defined
saved-search subsystem, and a per-auction digest that batches the
interesting lots in each upcoming sale.

**Status:** approved 2026-05-27. Three sequential PRs on `main`, in order:
PR-1 (tiered going-cheap), PR-2 (saved searches), PR-3 (per-auction digest).

This is the next chapter after the Direction A dashboard MVP (PRs through
`dashboard-watchlist-finish`, merged). It changes *what the system notifies
about and when*, not the dashboard's shape.

---

## Motivation

The current notifier evaluates each lot independently in real time. Two
problems:

1. **Going-cheap fires too early.** Auctions open at a nominal $100 starting
   bid days before close, so `price_deal_score` looks fantastic the moment a
   lot is ingested. Alerts arrive days out, when the price is meaningless.
2. **The unit of attention is wrong.** Cars are browsed auction-by-auction,
   not lot-by-lot. An auction is a coherent decision ("is it worth my Saturday
   morning"). A per-lot trickle never assembles that picture.

Two capabilities are also missing entirely:

3. **User-defined interest.** "Interesting" today is LLM-judged only
   (`rarity_score`). There is no way to say "tell me about every 60s–70s
   Mustang regardless of what the model thinks."
4. **Event-level look-ahead.** Nothing fires on the auction event itself —
   no "this sale is happening, here is what is in it."

## Philosophy of the pivot

- **Unit of attention shifts from lot → auction event.** The digest becomes
  the primary notification surface. Per-lot alerts survive only for the one
  genuinely time-critical signal (going-cheap, near close).
- **Thresholds become functions of time-to-decision.** Going-cheap thresholds
  fall as close approaches; the digest fires only when an auction is
  decision-relevant (~24h out).
- **"Interesting" is both user- and model-defined.** Saved-search matches
  (user) and `rarity_score` (LLM) sit side-by-side in the digest. Neither is
  privileged.

## Forward-compatibility constraint: a second intake channel

A private-sale intake channel (Craigslist / Kijiji / Facebook Marketplace) is
planned for later. It is a fundamentally different decision unit: no closing
time (no going-cheap, no tiering), no event grouping (no digest), and inverted
urgency — good deals vanish in minutes, so the right shape is a real-time
per-listing alert.

The one signal that crosses both channels is the **saved-search match**. This
design therefore builds the saved-search matcher (PR-2) to be source-agnostic,
so the future private-sale work is a new *adapter*, not a new subsystem. The
auction-specific pieces (PR-1, PR-3) make no concession — they stay
auction-only by definition.

The total architectural cost paid now for this: one `MatchableListing`
dataclass and one polymorphic foreign-key shape in PR-2 (Section 2). Net
positive even if private sales never ship — the dataclass makes the matcher
unit-testable in isolation.

---

## PR-1 — Tiered going-cheap

The single surviving per-lot real-time alert, retuned.

### Behavior

The current single-threshold check plus the closing-in-24h gate (which today
only applies to *unflagged* lots — watched lots bypass it) is replaced by a
tier table evaluated against time-to-close. Behavior unifies across watched
and unwatched lots; the watched-bypass is removed.

```python
GOING_CHEAP_TIERS: tuple[tuple[timedelta, float], ...] = (
    (timedelta(hours=1),  0.15),   # T-1h: marginal but imminent
    (timedelta(hours=6),  0.30),   # T-6h: solid deal
    (timedelta(hours=24), 0.50),   # T-24h: screaming deal
)
# beyond T-24h: no alert, regardless of score
```

`cheap_threshold(time_to_close)` returns the threshold for the closest tier
whose window contains `time_to_close`, or `None` if further out than the widest
tier. The trigger fires when `price_deal_score >= cheap_threshold(...)` and the
existing dedup gate holds (`cheap_notified_at` is null, OR a rescore
improvement exceeded `rescore_improvement_threshold` — preserves the
"deal got even better" re-fire).

### Scope

- **Auction-only by definition.** Private-sale listings have no closing time.
- No DB schema change. No new files.

### Files

- `src/carbuyer/apps/notifier/triggers.py` — tier evaluator replaces
  single-threshold logic (~30 lines changed).
- `tests/apps/notifier/test_triggers.py` — tier-boundary tests.

### Testing (TDD)

Each tier boundary at `window − 1min`, `window`, `window + 1min`, for both
score-below and score-meets-threshold. A watched-lot regression test confirms
the uniform behavior (no bypass). Existing rescore-improvement test stays
green.

### Risks

Over-suppressing alerts that fire usefully days out — but that is the
explicitly desired direction.

---

## PR-2 — Saved searches subsystem

Five sub-components: schema, source-agnostic matcher, worker, dashboard CRUD,
dashboard match-view.

### 2.1 Data model

Two new tables.

```
saved_searches
  id                     bigint  PK
  name                   text    NOT NULL
  is_active              bool    NOT NULL DEFAULT true

  -- vehicle filters (NULL = wildcard)
  make                   text                  -- case-insensitive eq
  model                  text                  -- case-insensitive eq
  trim                   text                  -- case-insensitive LIKE %trim%
  year_min               int                   -- inclusive
  year_max               int                   -- inclusive
  mileage_km_max         int                   -- inclusive
  title_status           text[]                -- ANY-OF
  condition_categorical  text[]                -- ANY-OF

  -- location & price filters
  province               text[]                -- ANY-OF on pickup_province
  max_all_in_cost_cad    int                   -- inclusive

  created_at             timestamptz NOT NULL DEFAULT now()
  updated_at             timestamptz NOT NULL DEFAULT now()

saved_search_matches
  id                bigint  PK
  saved_search_id   bigint  NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE
  source_kind       text    NOT NULL   -- "auction_lot" today; "private_listing" later
  source_id         bigint  NOT NULL   -- polymorphic — no FK constraint
  matched_at        timestamptz NOT NULL DEFAULT now()
  dismissed_at      timestamptz         -- user muted this match (kept for audit)

  UNIQUE (saved_search_id, source_kind, source_id)
  INDEX  (source_kind, source_id)
  INDEX  (saved_search_id, matched_at DESC) WHERE dismissed_at IS NULL
```

Polymorphic FK (`source_kind` + `source_id`) rather than per-source match
tables: one matcher, one match list, one digest query — no duplicated read
paths when private sales arrive. A `saved_search.targets` column ("which
sources does this search apply to") is deliberately **not** added until a
second source exists.

### 2.2 Match semantics

All non-null filter fields combine with AND; null = wildcard. String fields
are case-insensitive. List fields (`province`, `title_status`,
`condition_categorical`) use ANY-OF. Year is a closed `[year_min, year_max]`
range; either bound may be null.

**Exclusion:** matches against lots with `user_action = 'passed'` are
suppressed. Matches against `interested` / `bid_placed` / `purchased` still
surface (shown with the lot's current state).

**Matches are not retracted.** A match is recorded the first time a lot
satisfies every filter; it is never deleted if the lot's price later rises past
`max_all_in_cost_cad` (auction prices only climb). `matched_at` is a
point-in-time fact. A price drop that brings a previously-too-expensive lot
under the cap *does* create a new match (see Section 2.3, trigger 2). The
digest may annotate current price but does not re-filter on it.

Source-agnostic adapter:

```python
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
    return MatchableListing(
        source_kind="auction_lot",
        source_id=lot.id,
        make=lot.make, model=lot.model, year=lot.year, trim=lot.trim,
        mileage_km=lot.mileage_km,
        title_status=lot.title_status,
        condition_categorical=lot.condition_categorical,
        province=auction.pickup_province,
        all_in_cost_cad=lot.all_in_at_current_bid_cad,
        rarity_score=lot.rarity_score,
    )
```

`match_listing(listing: MatchableListing, search: SavedSearch) -> bool` is a
pure function, unit-testable without the database.

### 2.3 Worker: `apps/search_matcher/`

A new systemd unit, matching the existing app-per-process pattern (~150 lines).
Triggers via Postgres LISTEN/NOTIFY:

1. `enrichment_pending` → new/re-enriched lot: run all active searches against it.
2. `valuation_pending` → price change: re-run searches that filter on
   `max_all_in_cost_cad` (the only filter field that changes post-enrichment).
3. `saved_search_changed` → search created/updated: backfill that search
   against all currently-active lots.

Claim pattern (`SELECT … FOR UPDATE SKIP LOCKED`) matches valuator/notifier.
Idempotent insert: `ON CONFLICT (saved_search_id, source_kind, source_id) DO
NOTHING`.

### 2.4 Dashboard CRUD

New router `src/carbuyer/apps/dashboard/routers/searches.py`:

- `GET  /searches` — list: cards with name, active toggle, match-count badge,
  "N new since last visit".
- `GET  /searches/new` — empty form (opens in `#modal-slot`, reusing the
  place-bid modal pattern).
- `POST /searches` — create + redirect to detail.
- `GET  /searches/{id}` — detail: filter summary + paginated current matches
  (lot cards) + match-over-time activity log.
- `GET  /searches/{id}/edit` — populated form.
- `PATCH /searches/{id}` — update (HTMX card swap, then NOTIFY for re-match).
- `POST /searches/{id}/dismiss/{match_id}` — mute one match.
- `DELETE /searches/{id}` — cascade.

Form: multi-select dropdowns for title/condition/province; free-text for
make/model/trim (autocomplete from existing distinct values); number inputs
for year/mileage/cost. Advanced filters (mileage, title, condition) collapse
under an expander; make/model/year/province show by default.

### 2.5 Navigation

The Watchlist nav item gains a top-of-page sub-tab strip: **Lots** (today's
kanban) and **Searches** (the new views). Keeps the 6-item nav budget;
groups user-curated discovery (specific lots vs. patterns) under one parent.

`/watched` continues to work (sub-tabs are added without changing the base
URL contract).

### 2.6 Files

```
New:
  alembic/versions/<rev>_saved_searches.py
  src/carbuyer/db/saved_searches.py                (matcher pure fn + adapter)
  src/carbuyer/apps/search_matcher/__init__.py
  src/carbuyer/apps/search_matcher/__main__.py
  src/carbuyer/apps/search_matcher/worker.py
  src/carbuyer/apps/dashboard/routers/searches.py
  src/carbuyer/apps/dashboard/templates/pages/searches_list.html
  src/carbuyer/apps/dashboard/templates/pages/search_detail.html
  src/carbuyer/apps/dashboard/templates/partials/search_form.html
  src/carbuyer/apps/dashboard/static/css/components/searches.css
  deploy/systemd/carbuyer-search-matcher.service
  tests/db/test_saved_search_matcher.py            (pure, in-memory)
  tests/apps/search_matcher/test_worker.py         (DB)
  tests/apps/dashboard/test_searches.py            (DB)

Modified:
  src/carbuyer/db/models.py                        (+ SavedSearch, SavedSearchMatch, AuctionLot rel)
  src/carbuyer/apps/dashboard/templates/base.html  (Watchlist sub-tabs)
  src/carbuyer/apps/dashboard/static/css/tailwind.css (@import searches.css)
```

### 2.7 Testing

- **Unit (no DB):** `match_listing` — every filter field independently
  (wildcard, exact, range, ANY-OF, exclusion). ~30 cases.
- **Worker (DB):** lot insert → NOTIFY → match row appears; search insert →
  backfill; search delete → cascade.
- **Dashboard (DB):** form submit → row + NOTIFY; match view renders; dismiss
  works.

### 2.8 Risks

- **Backfill cost on search create** — ~5,000 evaluations is ~50ms in Python.
  Benchmark in the worker test, do not pre-optimize.
- **N×M evaluations on enrichment** — sub-second at realistic counts. No
  pre-optimization.
- **Form complexity** — mitigated by progressive disclosure (advanced filters
  collapsed).

---

## PR-3 — Per-auction digest

Composition only — queries data PR-2 and existing systems already produce.

### 3.1 Architecture

Cron-driven worker (matches `ingester` / `distiller` / `source_watchdog`).
New systemd timer fires every 15 minutes.

```sql
SELECT id FROM auctions
WHERE scheduled_start_at IS NOT NULL
  AND scheduled_start_at - now() <= INTERVAL '24 hours'
  AND scheduled_start_at > now()
  AND digest_sent_at IS NULL
  AND status NOT IN ('cancelled', 'past');
```

For each: build digest → if non-empty, post to Discord → set
`digest_sent_at = now()`.

### 3.2 Content composition

Three sections, priority order, deduplicated across sections (a lot in
section 1 never repeats in 2):

```
🎯 Saturday, Mar 7 · 10:00 AM CST
Graham Auctions — Headingley, MB
47 lots · 12 vehicles · auction page →

🔍 Your saved searches (2)
  ★ 1968 Ford Mustang Coupe · 87k km · Clean → /lots/123
  ★ 1973 Mustang Mach 1 · 154k · Project → /lots/124

✨ Rare / special vehicles (3)
  • 1988 Ferrari Mondial 3.2 → /lots/125
  • 2005 Dodge Viper SRT-10 → /lots/126
  • 1979 Lincoln Continental Mk V → /lots/127

💰 Cheap-deal alerts will arrive closer to close.
```

- **Section 1 (saved-search matches):** join through `auction_lots` —
  `saved_search_matches m JOIN auction_lots l ON m.source_kind = 'auction_lot'
  AND m.source_id = l.id WHERE l.auction_id = ? AND m.dismissed_at IS NULL AND
  l.user_action != 'passed'`, annotated with the matching search.
- **Section 2 (rare/special):** `rarity_score >= digest_rarity_threshold AND
  user_action != 'passed' AND id NOT IN <section 1>`. Threshold defaults near
  the existing `rarity_threshold`.
- **Truncation:** each section caps at 10 lots with "… and N more" → auction
  page. Respects Discord embed limits (25 fields, 1024 chars/field).
- **Skip empty:** both sections empty → no message, but `digest_sent_at` is
  still set (no re-evaluation for the next 24h).

### 3.3 Two-stage rarity: long-lead + digest

`early_warning` is kept but tightened: it fires only when `rarity_score >=
long_lead_threshold` (≈ top 5%) AND the auction is **≥7 days out** — "drop
everything, plan a road trip." The digest catches the bulk at T-24h. Two
signals, distinct purposes, no routine duplication.

### 3.4 Discord channel

`notifier/channel_resolver.py` gains an `"auction_digest"` routing key,
defaulting to the existing alerts channel. The knob exists to split it into a
dedicated channel later.

### 3.5 Dashboard preview page

`GET /auctions/{id}/digest` renders the digest as it would be (or was) sent —
live preview of the current composition. Useful for verifying a new search is
picked up and as an audit of what fired. This also lays the router groundwork
(`apps/dashboard/routers/auctions.py`) for the eventual `/auctions/{id}` event
view.

### 3.6 Data model

One column, no new tables.

```sql
ALTER TABLE auctions ADD COLUMN digest_sent_at timestamptz;
CREATE INDEX ix_auctions_digest_eligibility
  ON auctions (scheduled_start_at)
  WHERE digest_sent_at IS NULL AND scheduled_start_at IS NOT NULL;
```

### 3.7 Files

```
New:
  alembic/versions/<rev>_auction_digest.py
  src/carbuyer/apps/auction_digest/__init__.py
  src/carbuyer/apps/auction_digest/__main__.py
  src/carbuyer/apps/auction_digest/runner.py       (query → compose → post → mark)
  src/carbuyer/apps/auction_digest/composer.py     (pure: auction + lots → DigestEmbed)
  src/carbuyer/apps/dashboard/routers/auctions.py
  src/carbuyer/apps/dashboard/templates/pages/auction_digest_preview.html
  deploy/systemd/carbuyer-auction-digest.timer
  deploy/systemd/carbuyer-auction-digest.service
  tests/apps/auction_digest/test_composer.py       (pure, ~20 cases)
  tests/apps/auction_digest/test_runner.py         (DB + mocked Discord)
  tests/apps/dashboard/test_auction_digest_preview.py

Modified:
  src/carbuyer/db/models.py                        (+ Auction.digest_sent_at)
  src/carbuyer/apps/notifier/channel_resolver.py   (+ "auction_digest" key)
  src/carbuyer/apps/notifier/triggers.py           (early_warning long-lead gate)
  tests/apps/notifier/test_triggers.py             (early_warning regression)
  src/carbuyer/apps/dashboard/templates/base.html  (nav: auction page link target)
```

`discord_post.py` is reused as-is.

### 3.8 Testing

- **Composer (pure):** auction × match × rarity → expected `DigestEmbed`.
  ~20 cases: only-matches, only-rarity, both, neither (None), truncation at 10,
  dismissed excluded, passed-lot excluded.
- **Runner (DB + Discord mock):** eligible auction fires + marks sent; empty
  composition marks sent + no Discord call; already-sent no fire; past-start no
  fire.
- **Preview page (DB):** renders current composition; empty state.

### 3.9 Risks

- **Late-added lots missed** — caught by real-time saved-search match and
  going-cheap individually. Acceptable.
- **Spam on busy days** — one digest per event is the correct unit.
- **Missing `scheduled_start_at`** — those auctions get no digest; logged as a
  source data-quality issue.
- **`scheduled_start_at` drift after send** — no re-fire for now; a
  `digest_invalidated_at` reset can be added later if it becomes real.

---

## Out of scope (future, unblocked by this design)

- **Private-sale intake** (Craigslist / Kijiji / Facebook Marketplace): new
  ingestion pipeline, new `private_listings` table, a `PrivateListing →
  MatchableListing` adapter (saved-search matcher gains the source for free),
  and a real-time per-listing alert path (no tiering, no digest).
- **`/auctions/{id}` event view** (Direction A): the digest preview router is
  the groundwork; the full grouped-by-event browse view is separate.
- **Dedicated `#auction-digests` Discord channel**: the routing knob exists;
  splitting is a config change.
