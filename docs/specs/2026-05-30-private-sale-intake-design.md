# Private-Sale Intake — Design (Kijiji MVP)

**Goal:** Add a second intake channel for **private-party vehicle listings**
(starting with Kijiji), so the user is alerted in near-real-time when a private
listing is either a **saved-search match** or a **standout deal** — the
classes of car that never show up in auctions.

**Status:** draft 2026-05-30. The marquee next chapter after the
notification-pivot (PRs 1–3, merged). The notification-pivot was deliberately
architected toward this: PR-2 built the saved-search matcher source-agnostic
(`MatchableListing` + the polymorphic `saved_search_matches.source_kind/source_id`)
so that "a future private-sale source needs no new table" for matches.

This design **changes intake and alerting**, not the auction pipeline. Auctions
and private sales are different decision units; this spec keeps them in separate
tables and separate workers, sharing only pure library logic (`llm/`,
`scoring/`, the matcher) and the saved-search concept.

---

## Motivation

Auctions are one supply of used vehicles; private-party sales
(Kijiji / Facebook Marketplace / Craigslist) are a larger, faster-moving one
with an **inverted urgency model**: no closing clock, listings appear and sell
within hours, and a good deal is gone in minutes. The auction machinery —
going-cheap *tiering* (a function of time-to-close), the per-auction digest,
bid polling — is meaningless here. The one signal that crosses both channels is
the **saved-search match**; the one capability worth reusing is **valuation**
(is this priced below market?).

So the private-sale channel is a thin, self-contained path that reuses the
*pure* parts of the pipeline (LLM enrichment, comp-based valuation, the matcher)
and adds a different, simpler alert: fire immediately when a listing is a deal
or a match.

## Decisions (settled in brainstorming)

1. **Both signals.** Alert on a saved-search match OR a standout deal
   (`price_deal_score ≥` a private-deal threshold). Needs enrichment (to get
   normalized make/model/condition), valuation (deal score), and matching.
2. **Kijiji first.** Canada-focused, high used-vehicle inventory in Western
   Canada, scrape-friendly public pages (closest to the existing McDougall HTML
   source). FB Marketplace (anti-scraping) and Craigslist (thin local inventory)
   are out for the MVP.
3. **~15-minute cadence.** A dedicated systemd-timer worker polling the Kijiji
   vehicle search every ~15 min — the practical floor for polite scraping. Not
   the 6h auction ingester.
4. **Separate `private_listings` table** (not reusing `AuctionLot`). Clean
   semantic separation; uses the polymorphic `source_kind="private_listing"`
   that PR-2 built. The auction-only workers never touch private rows.
5. **One self-contained inline worker.** The `private_sale` worker owns the
   whole private pipeline inline per cycle — scrape → upsert → enrich → value →
   match → alert — calling `llm/`, `scoring/`, and `match_listing` as
   **library code**, NOT spinning up parallel LISTEN/NOTIFY workers. Fewest
   moving parts for a low-volume channel; the auction enricher/valuator/notifier
   workers are untouched.
6. **Full dashboard integration.** The saved-search match views (PR-2's
   `/searches/{id}` detail + the match-count/"N new" badges) render
   `private_listing` matches alongside auction matches, exercising the
   polymorphic design end-to-end. Plus a `/private` browse page.

### Reuse premise (validate during planning)

The design rests on the auction enricher and valuator being **thin
orchestration over pure libraries**: `llm/` (prompts, schemas, provider) for
enrichment and `scoring/` (comps, fair_value, landed_cost, score) for
valuation. The `private_sale` worker calls those libraries directly with
private-listing data — it does **not** refactor or import the auction
`enricher`/`valuator` workers. **Planning task 0 is to confirm the exact
library entry points** (the LLM enrichment function's input shape, and the
`scoring/` functions' signatures) and, if any logic is still trapped inside a
worker module rather than a library, extract it to `llm/`/`scoring/` as a small
prep step. If that extraction turns out large, we revisit; the brainstorming
assumed it is small because the workers were described as thin shells.

---

## Architecture

```
Kijiji search (HTML)
  │  every ~15 min (systemd timer)
  ▼
private_sale worker (apps/private_sale/)  ── one cron run, inline pipeline ──
  1. scrape    sources/kijiji  → RawPrivateListing[]   (reuses sources/http.py)
  2. upsert    private_listings (insert new / update changed → status pending; mark removed)
  3. for each listing with PENDING enrich/value status (this cycle's new+changed
     PLUS any left pending by a prior failed cycle), in its own short tx + outside-tx post:
       a. enrich   → call llm/  (normalize make/model/year, condition, flags, rarity)
       b. value    → call scoring/ (comps from historical_sales → expected_value,
                     all_in = ask + landed_cost, price_deal_score)
       c. match    → adapt_private_listing() → match_listing() vs active SavedSearches
                     → insert SavedSearchMatch(source_kind="private_listing")
       d. alert    → if (price_deal_score ≥ private_deal_threshold) OR (any match):
                     post a real-time message to the private_deals Discord channel,
                     stamp alerted_at  (dedup; re-alert only on a meaningful price drop)
  ▼
Discord  +  dashboard (/private browse, and private matches in /searches/{id})
```

The worker mirrors the **auction_digest cron pattern** (PR-3): `main(now=None)`
runs once and returns, `run_worker("private_sale", main)`, singleton advisory
lock, per-listing `try/except` isolation, **Discord POST outside the DB
transaction then stamp in a short second tx** (the duplicate-post lesson from
PR-3). Each listing's enrich/value/match happens in a short read/write tx; the
alert POST is outside it.

## Components

### 1. `private_listings` table (new)

Mirrors the *vehicle + enrichment + valuation* subset of `AuctionLot`, minus
everything auction-specific (bids, buyer premium, closing time, auction event).

```
private_listings
  id                     bigint PK
  source                 text   NOT NULL              -- "kijiji"
  source_listing_id      text   NOT NULL              -- platform listing id
  url                    text   NOT NULL
  canonical_url          text   NOT NULL
  photos                 text[] NOT NULL DEFAULT '{}'
  title                  text
  description            text
  pickup_province        text                          -- parsed from listing location
  pickup_city            text

  -- ask price (the listing's fixed price)
  ask_price_cad          numeric(12,2)

  -- vehicle identity (scraper on insert; enricher normalizes in place)
  year                   int
  make                   text
  model                  text
  trim                   text
  vin                    text
  mileage_km             int
  title_status           text   NOT NULL DEFAULT 'UNKNOWN'
  condition_categorical  text
  -- enricher outputs (mirror the AuctionLot enricher columns used by matcher/alert)
  red_flags / green_flags / showstopper_flags  jsonb NOT NULL DEFAULT '[]'
  rarity_score           double precision
  summary                text
  desirable_trim_or_spec / classic_or_collector  bool NOT NULL DEFAULT false

  -- valuator outputs
  expected_value_cad     numeric(12,2)
  all_in_cost_cad        numeric(12,2)                 -- ask + landed_cost (no BP/tax)
  price_deal_score       double precision
  flag_score             int
  confidence_bucket      text

  -- lifecycle + pipeline status
  enrichment_status      text   NOT NULL DEFAULT 'pending'
  valuation_status       text   NOT NULL DEFAULT 'pending'
  first_seen_at          timestamptz NOT NULL DEFAULT now()
  last_seen_at           timestamptz NOT NULL DEFAULT now()
  removed_at             timestamptz                    -- listing gone from Kijiji
  alerted_at             timestamptz                    -- real-time alert dedup
  last_alert_price_cad   numeric(12,2)                  -- to detect price-drop re-alert
  user_action            text                           -- interested/passed/etc. (dashboard)

  created_at / updated_at  timestamptz                  (TimestampMixin)

  UNIQUE (source, source_listing_id)
  INDEX  (price_deal_score)
  INDEX  (make, model, year)
  INDEX  on (id) WHERE enrichment_status='pending' OR valuation_status='pending'  -- claim
```

No FK to `auctions`/`auction_lots`. `user_action` reuses the `UserAction`
StrEnum (`interested`/`bid_placed`→ n/a/`purchased`/`passed`) for dashboard
parity; `passed` suppresses the listing from match/deal views like auction lots.

### 2. Kijiji source (`sources/kijiji/`)

A new plugin implementing a **listing role** (the `SourceType = "listing"` arm
already exists in `sources/base.py`). It reuses `sources/http.py`
(`make_client`, `RetryTransport`, `jittered_sleep`). It produces a
`RawPrivateListing` (a new raw dataclass in `sources/base.py`, parallel to
`RawLot` but private-shaped: url, photos, title, description, location, ask
price, and best-effort year/make/model/mileage from the listing). MVP scope:
the Western-Canada vehicle search results + per-listing detail pages; paginated
with jittered sleeps; province from `settings` (mirror `hibid_provinces`).

A `parse_listing_url(url)` lets a future paste-a-Kijiji-link flow resolve a
single listing. Registration mirrors the auction plugins (`register()` at module
bottom; the worker imports it).

### 3. The `private_sale` worker (`apps/private_sale/`)

- `__init__.py`, `__main__.py` (`run_worker("private_sale", main)`),
  `worker.py` (the cron `main(now=None)` + `run_cycle(now)` test seam),
  `pipeline.py` (the per-listing enrich→value→match→alert, the testable core).
- **Scrape + upsert:** an idempotent `INSERT ... ON CONFLICT (source,
  source_listing_id) DO UPDATE` (coalesce non-null), bumping `last_seen_at`;
  content change (price/title/description/photos) resets
  `enrichment_status`/`valuation_status` to pending (mirrors
  `upsert_lot_with_status_cascade`). Listings absent from the latest scrape that
  were present before get `removed_at` stamped (a sweep, bounded).
- **Enrich:** build a vehicle snapshot from the private_listing, call the `llm/`
  enrichment library (same prompts/schema as the auction enricher), apply the
  normalized fields + flags + rarity to the row, set `enrichment_status=done`.
- **Value:** call the `scoring/` libraries — comps from `historical_sales`,
  `expected_value_cad`, `all_in_cost_cad = ask + landed_cost` (buyer premium and
  taxes are zero for private sales; transport/inspection landed cost still
  applies), `price_deal_score`, `flag_score`, `confidence_bucket`. Set
  `valuation_status=done`.
- **Match:** `adapt_private_listing(listing) -> MatchableListing` (new, in
  `db/saved_searches.py`, `source_kind="private_listing"`), run vs all active
  `SavedSearch`es via the pure `match_listing`, idempotent-insert
  `SavedSearchMatch(source_kind="private_listing", source_id=listing.id)`.
- **Alert:** if `price_deal_score ≥ settings.private_deal_threshold` OR the
  listing produced ≥1 match (and `user_action != passed`), post a plaintext
  message (reuse `discord_post.post_simple_message`) to the resolved
  `private_deals` channel; stamp `alerted_at` + `last_alert_price_cad`.
  **Dedup:** alert once per listing; re-alert only if the price dropped by ≥ a
  configured fraction below `last_alert_price_cad` (the private analogue of the
  rescore-improvement re-fire).

### 4. Real-time alert + Discord channel

A new `"private_deals"` channel key in `DISCORD_CHANNELS`, resolved at the start
of each worker run (the digest's `_resolve_digest_channel` pattern), falling
back to the existing alerts channel (`early_warning`/`hot_deals`) when unset.
The message is plaintext (the codebase has no embeds): vehicle line, ask price,
expected value + deal score, condition/flags one-liner, the matching saved
search name(s) if any, and the listing link.

### 5. Full dashboard integration

- **`/searches/{id}` detail + badges** become **polymorphic**: the current
  queries join `auction_lots`; they are extended to ALSO surface
  `source_kind="private_listing"` matches by querying `private_listings` and
  merging (a unified "match row" carrying `{kind, lot_or_listing, search}`,
  ordered by `matched_at desc, id desc`). The `_match_count`/`_new_count` badges
  count both kinds (still excluding dismissed + `passed`). Templates render a
  private match with a `[private]` tag + the listing link.
- **`/private` browse page** (new dashboard router) — a simple feed of current
  (non-removed, non-passed) private listings with deal score, ask vs expected,
  and `interested`/`passed` actions (reusing the dashboard's action pattern).
  This is the private analogue of the watchlist's lot view.

## Data flow / reuse summary

| Concern | Private-sale path |
|---|---|
| Scrape | new `sources/kijiji` (reuses `sources/http.py`) |
| Persist | new `private_listings` table + upsert |
| Enrich | **reuse `llm/`** (same prompts/schema), applied to the new row |
| Value | **reuse `scoring/`** (comps/fair_value/landed_cost/score), zeroed premium |
| Match | **reuse `match_listing`** + new `adapt_private_listing` (source_kind="private_listing") |
| Alert | new immediate trigger; **reuse `discord_post.post_simple_message`** |
| Dashboard | extend PR-2 match views to be polymorphic + new `/private` page |
| Orchestration | one new inline cron worker (the digest pattern) |

## Explicitly out of scope (auction-only)

Bid polling, going-cheap **tiering** (a closing-clock function), the
closing-soon/lot-extended/early-warning triggers, the per-auction digest, the
`auctions`/`auction_lots`/`auction_bid_history` tables. Private listings never
flow through any of these — separate table, separate worker, no NULL-guard
reliance.

Also deferred beyond this MVP: Facebook Marketplace / Craigslist sources
(add as more `sources/*` plugins + worker strategies); a paste-a-link flow; a
private-vs-auction comp-channel weighting refinement (see Risks).

## Error handling

- Per-listing `try/except` isolation in the worker loop (one bad listing never
  aborts the cycle); structured logging with the listing id; summary counters.
- Discord POST **outside** the DB transaction, then stamp `alerted_at` in a
  short second tx with a re-check (PR-3's duplicate-post fix).
- Scrape failure (Kijiji layout change / block): the cycle logs + exits
  cleanly; `source_watchdog` can later be taught the `private_sale` channel.
- LLM / valuation failure on one listing: leave its status pending (retried next
  cycle), count as failed — no silent success.
- Channel unresolved (no `private_deals` and no fallback): log an error, skip
  alerting for the cycle (still ingests/enriches/values/matches).

## Testing

- **Kijiji parser (pure):** fixture HTML → `RawPrivateListing` (search page +
  detail page); robust to missing fields.
- **Pipeline (DB + mocked LLM/Discord):** new listing → enriched → valued →
  matched → alerted + `alerted_at` stamped; deal-but-no-match alerts;
  match-but-no-deal alerts; neither → no alert; `passed` listing → no alert;
  price-drop re-alert; removed listing → `removed_at`; idempotent re-scrape.
- **`adapt_private_listing` (pure):** field mapping → `MatchableListing`,
  `source_kind="private_listing"`.
- **Dashboard (DB):** a `private_listing` match renders in `/searches/{id}`;
  badges count both kinds; `/private` browse renders + actions work; `passed`
  excluded everywhere.
- **Schema/migration:** `private_listings` model ↔ migration parity.

## Risks

- **Kijiji scraping fragility / blocking** — HTML layout changes or anti-bot.
  Mitigated by `RetryTransport`, polite jittered cadence, and isolating scrape
  failures. The highest-risk piece; gate the MVP on a working parser against
  saved fixtures.
- **Private-vs-auction comp bias** — `historical_sales` comps are
  auction-derived; private-party *resale* value is the right benchmark for "good
  deal", and private asks run higher than auction hammers. MVP uses the existing
  `expected_value` as the benchmark and flags this; a `sale_channel`-aware comp
  weighting is a follow-on refinement, not MVP.
- **Alert spam on first run** — the first scrape sees the entire current
  inventory as "new". Mitigate: on the very first cycle (or backfill), only
  alert on matches/deals above threshold, and/or cap alerts per cycle with a
  logged truncation (no silent drop).
- **Library-reuse assumption** — see "Reuse premise"; planning validates the
  `llm/`/`scoring/` entry points before committing to the inline design.

## Implementation phasing (for the plan stage)

Likely decomposes into sequential PRs (like the notification-pivot):
1. **Foundation** — `private_listings` schema + migration; `RawPrivateListing`
   + the `adapt_private_listing` adapter; confirm/extract the `llm/`+`scoring/`
   library entry points.
2. **Kijiji source + worker** — `sources/kijiji` parser; the `private_sale`
   inline worker (scrape→enrich→value→match→alert) + systemd timer + the
   `private_deals` channel + alert.
3. **Dashboard** — polymorphic match views (`/searches/{id}` + badges) +
   `/private` browse page.

The writing-plans step will turn the chosen slice(s) into task-by-task plans.
