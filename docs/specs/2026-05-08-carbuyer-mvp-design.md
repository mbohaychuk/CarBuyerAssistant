# CarBuyerAssistant — MVP design

**Date:** 2026-05-08
**Status:** Approved — auction-focused MVP
**Scope:** Personal Canadian used-vehicle auction deal-finder. Western Canada–focused.

---

## 1. Goals & non-goals

### Goals (MVP)

- A personal tool that compiles vehicle lots across Western Canadian auction sites — small farm/estate auctions and larger commercial auctioneers — into a single ranked feed.
- **Two distinct alert types:**
  - **Rare/popular early-warning** — fired when a desirable vehicle is detected at discovery (typically days–weeks before auction close), so the user has time to travel for inspection.
  - **Going-cheap price alert** — fired when a lot's all-in cost (current bid + buyer's premium + tax + landed cost) sits meaningfully below estimated fair value, especially as auction close approaches.
- Three user-action tiers — `interested` (top of dashboard, all alerts), `maybe` (still tracked, alerts only on going-cheap), `not_interested` (suppressed) — plus a default unflagged state that gets cheap alerts only when a lot is closing soon.
- **Comp comparison feature in the dashboard:** when viewing any lot, surface matching vehicles from `historical_sales` (sold) and currently-open `auction_lots`, side-by-side, for in-context price reference.
- Track bid trajectory by polling current bid + end time over the lot's life. Reconstruct what auctions don't expose publicly.
- Honor HiBid soft-close mechanics — never assume the nominal end time is final.
- LLM description enrichment on every lot: structured red/green flags, condition bucket, normalized vehicle facts, and **rarity/desirability assessment**.
- Nightly vision-LLM pass on the price-shortlist (top ~10% by deal score) to assess condition from photos.
- Build a cumulative comp database from auction outcomes; use it to refine fair-value estimates over time.
- Track legal flipping volume (transfers per calendar year) with curbsider-threshold warnings.
- Factor transport cost and per-province inspection cost into deal scoring (rural pickups are expensive).
- Extract Carfax data opportunistically when sellers link or attach it.

### Non-goals (explicit)

- **Kijiji, AutoTrader.ca, Facebook Marketplace, or any retail-listing source** as deal-finding sources. Deferred to phase 2 as comp-data augmentation only. Rationale: Kijiji's deal density has dropped, AutoTrader is dealer markup at retail, FB Marketplace is hostile to scraping. Auctions are where deals live.
- Salvage / insurance auctions (Copart, IAA) — different scoring entirely, deferred.
- Government surplus auctions (govdeals.ca, BC Auctions, Alberta surplus) — phase 2.
- Classics as a separate sub-product — handled inline via rarity scoring; deeper specialty (BaT-style auctions, Hagerty integration) deferred.
- EVs and motorcycles.
- Multi-user accounts, family/friend wishlists.
- Real-time vision analysis.
- Cross-border (US) auctions.

### Operating constraints

- Runs on an always-on Linux machine at home. Cloud-portable from day one (env-driven secrets, no localhost-hardcoded URLs, exportable Postgres).
- Residential IP for all scraping. Per-source conservative cadence; tiered polling for bid updates.
- LLM costs targeted under $20/month at MVP volume.

---

## 2. System architecture

### Shape

Staged pipeline of independent workers + dashboard + Discord bot, all communicating through one Postgres. Workers are independent processes; failure of any one stage doesn't cascade.

```
                       ┌────────────────┐
                       │   Postgres     │  (source of truth, append-mostly)
                       └────────────────┘
                               ▲ ▼
   ┌──────────────────┐  ┌────────────────────┐  ┌────────────────────┐
   │ Auction-         │  │ Lot-scraper        │  │ Description        │
   │ discoverer       │→│ (queue)            │→│ enricher (queue)   │
   │ (timer, 4×/day)  │  └────────────────────┘  └────────────────────┘
   └──────────────────┘  ┌────────────────────┐  ┌────────────────────┐
   ┌──────────────────┐  │ Valuator           │  │ Notifier           │
   │ Bid-poller       │→│ (queue)            │→│ (queue)            │
   │ (continuous,     │  └────────────────────┘  └────────────────────┘
   │  tiered cadence) │  ┌────────────────────┐  ┌────────────────────┐
   └──────────────────┘  │ Vision-batcher     │  │ Auction-distiller  │
                         │ (timer, nightly)   │  │ (timer, nightly)   │
                         └────────────────────┘  └────────────────────┘
                         ┌────────────────────┐  ┌────────────────────┐
                         │ Dashboard          │  │ Discord bot        │
                         │ (continuous)       │  │ (continuous)       │
                         └────────────────────┘  └────────────────────┘
```

### Worker cadence and execution model

| Worker | Cadence | Execution |
|---|---|---|
| Auction-discoverer | 4×/day | systemd timer + oneshot |
| Lot-scraper | continuous (queue) | systemd `Type=simple, Restart=always` |
| Description enricher | continuous (queue) | systemd `Type=simple, Restart=always` |
| Valuator | continuous (queue) | systemd `Type=simple, Restart=always` |
| Bid-poller | continuous, tiered cadence | systemd `Type=simple, Restart=always` |
| Vision-batcher | nightly 02:00 | systemd timer + oneshot |
| Auction-distiller | nightly 03:00 | systemd timer + oneshot |
| Notifier | continuous (queue) | systemd `Type=simple, Restart=always` |
| Dashboard | continuous | systemd `Type=simple, Restart=always` |
| Discord bot | continuous | systemd `Type=simple, Restart=always` |

All units depend on the local Postgres unit; logs flow to journald (`journalctl -u <name>`).

### Inter-worker coordination

- Queue listeners watch for `*_status='pending'` rows. Wakeup is via Postgres `LISTEN/NOTIFY` channels signaled by the upstream worker; the listener claims work via `SELECT … FOR UPDATE SKIP LOCKED`, processes, commits, then waits.
- A dedicated psycopg3 `AsyncConnection` (autocommit, separate from the SQLAlchemy pool) handles `LISTEN`. The SA pool handles all other queries.
- For singleton operations (e.g. dashboard-triggered "rescore everything") use `pg_advisory_xact_lock(hashtext(...))` to serialize across workers.

### Data flow (one lot's lifecycle)

1. Auction-discoverer hits each source plugin (HiBid Western CA province pages, farmauctionguide.com per-province feeds, McDougall direct, Ritchie Bros catalog, Michener Allen catalog), upserts `auctions` rows for new/updated auctions.
2. Lot-scraper claims new auctions, fetches each catalog, writes `auction_lots` with `enrichment_status='pending'` and emits `NOTIFY enrichment_pending`.
3. Description enricher claims each lot, runs the LLM (description + rarity assessment), writes structured output, sets `enrichment_status='done'`, `valuation_status='pending'`, emits `NOTIFY valuation_pending`.
4. Valuator claims the lot, builds the comp set from `historical_sales` + recently-closed `auction_lots`, computes fair-value range and `price_deal_score`, computes `rarity_score` (combining LLM output and database signals). Sets `valuation_status='done'`, emits `NOTIFY notification_pending`.
5. Notifier evaluates the lot against trigger rules (§4h):
   - **Early-warning** if `rarity_score ≥ threshold` and lot closes ≥48h from now.
   - **Going-cheap** if `price_deal_score ≥ threshold` and other criteria match.
   - Both can fire for the same lot; they're separate events on separate channels.
6. Bid-poller maintains a tiered priority queue keyed on `next_poll_at`. Records observations in `auction_bid_history`. Tiers: `>24h to close → 60 min`; `2–24h → 15 min`; `1–2h → 5 min`; `10–60 min → 60 s`; `<10 min or extended → 30 s`, continuing past `scheduled_end` until `lot_status=closed`.
7. Vision-batcher (nightly) takes the day's price-shortlist (top ~10%), runs the two-pass vision LLM, writes vision findings; if findings contradict description condition by ≥2 buckets, emits a follow-up Discord message.
8. When a lot's status flips to `closed`, bid-poller records the final price.
9. Auction-distiller (nightly) finds lots ≥14 days past `closed_at`, copies distilled fields to `historical_sales`, deletes the `auction_lots` row — except `was_purchased_by_us=true` rows (kept forever) and `user_action ∈ ('interested', 'maybe')` rows (kept 90+ days).

---

## 3. Data model & retention

### `auctions` — auction events

- `id`, `source` (`hibid` / `mcdougall` / `ritchie_bros` / `michener_allen` / `farmauctionguide_discovered`), `source_auction_id`, `url`
- `auction_subtype` ∈ {`estate`, `commercial`} — drives the channel multiplier when a lot eventually distills. Defaults: HiBid + farmauctionguide-discovered + McDougall + Michener Allen → `estate`; Ritchie Bros → `commercial`. Auctioneer-name regex overrides apply.
- `auctioneer_name`, `auctioneer_external_id`
- `title`, `description`, `terms_text`
- `scheduled_start_at`, `scheduled_end_at`, `last_seen_end_at`, `closed_at`
- `pickup_address`, `pickup_city`, `pickup_province`, `pickup_window_text`
- `buyer_premium_pct`, `online_bidding_fee_pct`
- `gst_pct`, `pst_pct` (province-derived but per-auction-overridable)
- `status` ∈ {`upcoming`, `live`, `closing`, `closed`, `cancelled`}
- `first_seen_at`, `last_seen_at`, `discovery_confidence`

### `auction_lots` — individual vehicles

- `id`, `auction_id` (FK), `source_lot_id`, `lot_number`, `url`
- `title`, `description`, `photos` (URL array — never bytes)
- Normalized vehicle facts: `year`, `make`, `model`, `trim`, `engine`, `transmission`, `drivetrain`, `mileage_km`, `vin`, `title_status`, `province_of_origin`
- LLM enrichment: `condition_categorical`, `condition_confidence`, `red_flags` (jsonb), `green_flags` (jsonb), `showstopper_flags` (jsonb), `enrichment_status`, `enrichment_version`, `summary`
- **Rarity assessment:** `desirable_trim_or_spec` (bool), `classic_or_collector` (bool), `desirability_signals` (jsonb), `desirability_evidence` (jsonb verbatim quotes), `historical_comp_count` (int, computed by valuator), `recent_appreciation` (float, nullable, phase-2 calibration), `rarity_score` (computed)
- Vision findings (when run): `vision_findings` (jsonb), `vision_condition_overall`, `vision_confidence`, `vision_contradictions`
- Auction-specific bid state:
  - `current_high_bid_cad` (nullable until first bid)
  - `last_bid_observed_at`, `bid_count_visible`, `reserve_met` (`null` / `true` / `false`)
  - `lot_status` ∈ {`open`, `closing_soon`, `extended`, `closed`, `unsold`, `sold`}
  - `closed_at`, `final_bid_cad`
- Valuation:
  - `comp_count`, `value_low_cad`, `value_mid_cad`, `value_high_cad`, `expected_value_cad`
  - `landed_cost_premium_cad`
  - `all_in_at_current_bid_cad`
  - `recommended_max_bid_cad`
  - `price_deal_score` (computed at current bid)
  - `flag_score`, `confidence_bucket`, `suspicious_underprice_flag`
  - `scoring_version`, `weights_hash`
- Notification state per trigger: `early_warning_notified_at`, `cheap_notified_at`, `closing_notified_at`, `last_notified_channel`
- User action: `user_action` ∈ {`null`, `interested`, `maybe`, `not_interested`}, `notes`, `was_purchased_by_us`

### `auction_bid_history` — observed bid trajectory

- `id`, `lot_id` (FK), `observed_at`
- `current_high_bid_cad`, `end_time_at_observation`, `status_at_observation`
- Append-only. Source for trajectory plotting and bid-velocity signals.

### `historical_sales` — distilled comp database

Schema-versioned. Distillation copies these fields:

- Vehicle: `year`, `make`, `model`, `trim`, `engine`, `transmission`, `drivetrain`, `mileage_km`, `vin`, `title_status`, `province_of_origin`
- `condition_categorical`
- `final_listed_price_cad`, `days_listed` (for auctions: `final_bid_cad` and lot duration)
- For auctions: `buyer_premium_pct_at_sale`, `final_price_with_premium_cad` (the actual all-in clearing price)
- `sale_channel` ∈ {`auction_estate`, `auction_commercial`, `auction_govt`, `private`, `dealer`, `other`} — in MVP only the auction values populate
- `sale_platform` (string, indexed) — `hibid`, `mcdougall`, `ritchie_bros`, `michener_allen`, etc.
- `seller_province`, `seller_city`
- `observed_first_at`, `disappeared_at`
- `disposition_reason` ∈ {`sold`, `unsold`, `cancelled`, `unknown`}
- Score-feedback: `was_notified` (bool), `was_purchased_by_us` (bool), `notes`
- `schema_version`

### `purchases` — vehicles I actually buy (legal-tracking)

- `purchase_date`, `sale_date` (nullable), `make`, `model`, `year`, `purchase_price_cad`, `sale_price_cad`, `province_of_purchase`, `province_of_sale`, `transport_cost_cad`, `inspection_cost_cad`, `repair_cost_cad`, `notes`, `linked_lot_id`
- Used by the dashboard to compute YTD transfer count and warn near curbsider-license thresholds.

### `searches` — reserved for phase 2

Single MVP row. `searches.user_id` exists, hardcoded.

### Indexes (initial)

- `auctions (status, scheduled_end_at)` — find lots closing soon
- `auction_lots (auction_id, lot_status)`
- `auction_lots (make, model, year)` — comp lookups
- `auction_lots (price_deal_score DESC, lot_status)` — dashboard feed
- `auction_lots (rarity_score DESC, scheduled_end_at)` — early-warning triage
- `auction_lots (enrichment_status / valuation_status / notification_status)` — partial indexes on `pending` for queue claims
- `auction_bid_history (lot_id, observed_at)` — trajectory queries
- `historical_sales (make, model, year, mileage_km)` — comp lookups

### Retention rules

- `auction_lots` rows distill into `historical_sales` 14 days after `closed_at`, **except**:
  - `was_purchased_by_us=true` → kept forever in `auction_lots`.
  - `user_action ∈ ('interested', 'maybe')` → kept 90+ days, then distilled with the `user_action` retained as a feedback signal.
- Photo URLs go stale when the auction's host site cycles them; never re-fetched. Vision LLM extracts structured findings; raw images aren't stored.

---

## 4. Scoring

Two independent scores, combined via the trigger logic in §4h:

### 4a. Comp set

For a target lot:
- Same `make` + `model` + `trim`
- `year` within ±1 (±2 for fallback when comp count is low)
- `mileage_km` within ±20%
- Same province preferred; widen to all-of-Canada if fewer than 5 comps
- Drop comps with `mileage_km` z-score outside ±2 within the comp set

Pull from `historical_sales` (true clearing-price proxies) + closed `auction_lots` within 14 days of close (not yet distilled). Open `auction_lots` are NOT used as comps until they close.

If fewer than 5 comps survive trimming: `confidence_bucket='insufficient'`, no `price_deal_score`. **Rarity score still computes** and may still trigger early-warning.

### 4b. Channel normalization (private-sale-equivalent)

Every comp price is normalized to private-sale-equivalent before percentile computation. In MVP, all comps are auctions, so the multipliers convert the cleared-at-auction price to what the same vehicle would have cleared at as a private sale.

| Source channel | Multiplier |
|---|---|
| `private` | 1.00 (reference; phase-2 only) |
| `dealer` | 0.92 (phase-2) |
| `auction_estate` | 1.20 |
| `auction_govt` | 1.15 (phase-2 source) |
| `auction_commercial` | 1.10 |

`expected_value` is therefore always *what the vehicle would clear at in a private sale*. This is the right reference because flipping math assumes the user resells privately.

Multipliers are MVP defaults; calibration phase 2 learns them from same-vehicle cross-channel observations.

### 4c. Asking-to-sold haircut (phase-2 only)

Only applies to `active_listings` comps, which don't exist in MVP. Reserved for when listings come back as comp augmentation.

### 4d. Fair-value range

From the comp set (after channel normalization):
- `value_low = comp_P10`, `value_mid = comp_P50`, `value_high = comp_P90`

Map this lot's LLM-assessed condition to a position in the range:

| Condition | Position |
|---|---|
| bad | 0% (`value_low`) |
| poor | 25% |
| decent | 50% (`value_mid`) |
| good | 75% |
| great | 100% (`value_high`) |

`expected_value = value_low + position × (value_high - value_low)`.

### 4e. Landed cost premium

```
transport_estimate = max(600, 400 + 0.65 × distance_km)
inspection_cost_by_dest = {AB: 200, ON: 120, BC: 125, QC: 125, MB: 100, SK: 75, NS: 50, NB: 50, others: 75}
repair_contingency_by_dest = {AB: 350, ON: 350, QC: 250, others: 150}
landed_cost_premium = transport_estimate + inspection_cost_by_dest[dest] + repair_contingency_by_dest[dest]
```

Same-province lots: `landed_cost_premium = 0`. Critical input for rural auction pickups — a Toronto buyer winning a Calgary auction lot pays ~$1,750–2,400 transport plus inspection.

### 4f. Price-deal score

```
all_in_at_bid = current_high_bid × (1 + buyer_premium_pct) × (1 + gst_pct + pst_pct)
all_in_total  = all_in_at_bid + landed_cost_premium
price_deal_score = (expected_value - all_in_total) / expected_value
```

Positive = underpriced after all costs. Threshold for going-cheap notification: 0.15 (≥15% below adjusted fair value).

`suspicious_underprice` flag fires (does not block notification, just labels) when `current_high_bid < value_low × 0.85` and the lot has photos / is otherwise valid.

### 4g. Rarity / desirability score

The user's distinction matters: low comp count alone isn't a rarity signal — many vehicles have low sales because nobody wanted them. Rarity = unusual + desired. The score combines LLM judgment with database signals, gated on desirability.

**LLM-derived signals** (output by the description pass):
- `desirable_trim_or_spec` (bool): TRD Pro, Raptor, Z71, Limited, manual transmission on a mostly-automatic model, rare engine options, special editions. The LLM gets a curated taxonomy in the prompt.
- `classic_or_collector` (bool): pre-2000 generally, plus specific 2000–2010 model+spec combinations that are now sought-after (first-gen Tundra, last solid-axle Land Cruiser, manual 4Runners, Land Rover Defender, certain JDM imports, project-grade pickups). Era-and-model specific, not a year cutoff.
- `desirability_signals` (list of strings): "Manual transmission on a model that's 90% automatic", "First-gen Tundra known to last 500k+ km", "Low-mileage example of a desirable platform".
- `desirability_evidence` (list of verbatim quotes from the listing).

**Database-derived signals** (computed by the valuator):
- `historical_comp_count` (int): number of `historical_sales` rows matching the comp criteria.
- `recent_appreciation` (float, nullable): slope of comp prices over the last 12 months, if enough data. Null in MVP until the database matures.
- `low_comp_count_with_desirability` (bool): `historical_comp_count < 3` AND (`desirable_trim_or_spec` OR `classic_or_collector`). Genuinely rare AND desired.
- `low_comp_count_undesirable` (bool): `historical_comp_count < 3` AND no LLM desirability signals. NOT a rarity signal — these are unwanted vehicles, not rare ones.

**Rarity score formula:**

```
rarity_score = (
    2.0 if low_comp_count_with_desirability else 0
  + 1.5 if classic_or_collector else 0
  + 1.0 if desirable_trim_or_spec else 0
  + 1.0 if recent_appreciation > 0.05 else 0   # 5%/yr+ appreciation
)
# Clamp to [0, 5]
```

Weights are MVP defaults. Calibration phase 2 tunes them based on which early-warning notifications resulted in user `interested` actions and which didn't.

### 4h. Notification triggers

Five distinct trigger types; a lot can produce multiple notifications over its life:

**Early-warning (rare/popular)**
- `rarity_score ≥ 2.0`
- Lot closes ≥48h from now
- Not already early-warning-notified
- `user_action ≠ 'not_interested'`
- Channel: `#early-warning`

**Going-cheap (price deal)**
- `confidence_bucket ∈ {medium, high}` (≥5 comps)
- `price_deal_score ≥ notify_threshold` (default 0.15)
- `flag_score ≥ -1`, no showstopper flags
- For `user_action ∈ {'interested', 'maybe'}`: any time
- For unflagged lots: only when closing-soon (≤24h to close)
- For `user_action='not_interested'`: never
- Re-fires only on price improvement of ≥0.05 score from previous notification
- Channel: `#hot-deals` (score ≥0.20) or `#watchlist` (0.15–0.20)

**Auction closing-soon** (time-based, watched lots only)
- `user_action ∈ {'interested', 'maybe'}`
- Fires at T−24h, T−6h, T−1h
- Channel: `#auction-closing`

**Bid trajectory** (watched lots only)
- `user_action ∈ {'interested', 'maybe'}`
- Current bid crosses 80% of `recommended_max_bid`
- Channel: `#auction-closing`

**Lot extended** (soft-close)
- `user_action ∈ {'interested', 'maybe'}`
- Soft-close pushes end past nominal `scheduled_end`
- Channel: `#auction-closing`

Quiet hours (22:00–08:00 local) batch all but `early-warning` lots whose close is genuinely imminent. Early-warning fires immediately because that's the whole point — lead time matters.

### 4i. Recommended max bid

Working backwards from a target margin:

```
flip_margin     = max(1500, 0.10 × expected_value)        # config
target_all_in   = expected_value − flip_margin
recommended_max_bid =
    (target_all_in − landed_cost_premium)
    / ((1 + buyer_premium_pct) × (1 + gst_pct + pst_pct))
```

Surfaced prominently in the lot detail view: *"Don't bid above $X to keep your $1,500 margin."*

### 4j. Score versioning

Every scored lot records `scoring_version` and `weights_hash`. Weight changes don't retroactively rewrite history; backfill is an explicit admin action. Lets us A/B-test future weight changes against `historical_sales` outcomes.

### 4k. Calibration (phase 2)

When `historical_sales` and notification-outcome data accumulate:
- Replace P10/P90 spread with median-price-per-condition-bucket curve, learned per make/model.
- Calibrate channel multipliers from same-vehicle cross-channel sales (when listings come back as comps in phase 2).
- Calibrate flag weights from cross-correlation with `final_bid_cad` and `disposition_reason`.
- **Calibrate rarity weights** from notification outcomes: which early-warning lots actually attracted premium prices, which didn't, which `interested` flags were honored vs ignored.
- Compute `recent_appreciation` per make/model/year cohort once enough cross-time data exists.

---

## 5. LLM enrichment

### 5a. Provider abstraction

```
LLMProvider (ABC)
  describe(lot_input) -> EnrichmentOutput
  vision(photos, context) -> VisionOutput

OpenAIProvider          # MVP — gpt-4o-mini for both passes
AnthropicProvider       # alternative
OllamaProvider          # local, phase 2
```

Provider selected per stage from config. Cost ceiling enforced inside each provider; rate-limit errors trigger configurable fallback chain.

### 5b. Description pass (real-time, every lot)

**Inputs:**
- Lot title + description + structured scraper fields
- Auction context: `auction_subtype`, `buyer_premium_pct`, location, `pickup_window_text`
- Carfax URL if extractable from description
- Pre-prompt knowledge blocks:
  - Canonical taxonomy of red/green flags (with weights)
  - Model-specific gotchas table (Tacoma frame rust 2005–2015, CR-V 1.5T fuel-in-oil 2017–2022, Ford 3.5L EcoBoost cam phaser, Subaru EJ25 head gaskets, Hyundai/Kia Theta II rod bearings, Nissan CVT, etc.) — keyed by make/model/year so the LLM only sees relevant entries
  - Auction-specific scam patterns (no-engine-bay-photos, "ran when parked", titles-not-disclosed, withdrawn-from-prior-auction signals)
  - **Desirable-trim taxonomy:** the curated list of trims/specs/spec-combos that count as `desirable_trim_or_spec=true` (TRD Pro, Raptor, Z71, manual+4×4, rare engine options, special editions per make/model)
  - **Classic/collector taxonomy:** pre-2000 default, plus the era-and-model exception list for 2000–2010 vehicles that have entered collector territory

**Output schema (Pydantic v2 → OpenAI structured output):**

```python
class NormalizedVehicle(BaseModel):
    year: int
    make: str
    model: str
    trim: str | None
    engine: str | None
    transmission: Literal["manual", "automatic", "cvt", "unknown"]
    drivetrain: Literal["fwd", "rwd", "awd", "4wd", "unknown"]
    mileage_km: int | None
    vin: str | None

class FlagInstance(BaseModel):
    flag: str
    evidence: str       # verbatim quote
    weight: int

class RarityAssessment(BaseModel):
    desirable_trim_or_spec: bool
    classic_or_collector: bool
    desirability_signals: list[str]
    desirability_evidence: list[str]    # verbatim quotes

class EnrichmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    normalized_vehicle: NormalizedVehicle
    title_status: Literal["NORMAL", "SALVAGE", "REBUILT", "NON_REPAIRABLE", "STOLEN", "UNKNOWN"]
    condition_categorical: Literal["bad", "poor", "decent", "good", "great"]
    condition_confidence: float = Field(ge=0, le=1)
    red_flags: list[FlagInstance]
    green_flags: list[FlagInstance]
    showstopper_flags: list[FlagInstance]
    carfax_url: str | None
    summary: str
    rarity: RarityAssessment
```

**Prompt rules:**
- "Output `unknown` if you cannot determine the field. Do not guess."
- "If `condition_confidence < 0.5`, output `condition_categorical = decent`."
- "Quote evidence verbatim. Do not paraphrase."
- "Use only flags from the provided taxonomy. Do not invent new ones."
- "For `desirable_trim_or_spec`, set `true` only if the trim/spec appears in the provided desirability taxonomy. Do not invent desirability."
- "For `classic_or_collector`, set `true` if year ≤ 2000 OR if the year/make/model/trim combination appears in the classic-exception list."

**Carfax extraction:** if `carfax_url` is set, fetch the page and run a second small LLM call producing `CarfaxFindings` (accident_count, accident_severity_max, service_record_density, ownership_count, title_brands, odometer_consistency). Findings convert to additional flags.

### 5c. Vision pass (nightly, shortlist only)

Two-pass pattern (research-driven — VLMs underperform on multi-image reasoning):

**Pass 1 — per image:** classify shot type, image quality, list findings with severity + confidence + reasoning + explicit-unknowns. Sonnet-class for judgment.

**Pass 2 — gallery aggregation, given Pass-1 JSON only (no images):** coverage gaps, cross-panel paint consistency, staging signals, overall flags. Haiku-class is fine.

[Schemas as in prior auction spec. Photo prep: download fresh, resize 1024px long edge JPEG, cap at 8 photos per lot.]

**Reconciliation rule:** when `vision_confidence > 0.7` and `vision_condition` differs from `description_condition` by ≥2 buckets, the scorer uses `min(description_condition, vision_condition)` (pessimistic) and adds a `description_oversells_condition` red flag (-2).

### 5d. Re-enrichment

- Description pass re-runs if the listing's description text changes (sometimes editors flush new flags into edited descriptions).
- Vision pass does not re-run unless the photo URL set hash changes.

---

## 6. Notifications

### 6a. Discord bot, channel split

- `#early-warning` — rare/popular detected at discovery, ≥48h to close
- `#hot-deals` — going-cheap, score ≥0.20
- `#watchlist` — going-cheap, 0.15–0.20
- `#auction-closing` — time-based urgency for watched lots, soft-close extensions, bid-trajectory crosses
- `#auction-watch` — daily summary of watched lots
- `#vision-updates` — overnight vision contradictions / confirmations
- `#system-health` — scraper failures, rate-limit warnings, weekly summaries, curbsider check-ins

Bot connects out via TCP 443 (no inbound, no port forwarding). Uses `discord.py v2.x`. Slash commands only; intents minimized. Persistent views via `discord.ui.DynamicItem` with regex `custom_id` template (`deal:{action}:{lot_id}`); re-registered in `setup_hook` on every boot.

### 6b. Message format

Embedded card with thumbnail, title, current bid + all-in cost, fair-value range bar, score breakdown, top flags, location, end time, action buttons.

**Early-warning variant** (header style differs):
```
⭐ RARE FIND — 1985 Toyota Land Cruiser FJ60 (Edmonton, AB)
Closes Mar 15 (12 days from now) at Mack Auctions
Current bid: $5,500 · Estimated value: $18,000–$28,000
Rarity: classic Land Cruiser, manual transmission, Western Canada origin
[ View lot ]  [ 👍 Interested ]  [ 🤔 Maybe ]  [ 👎 Not interested ]
```

**Going-cheap variant:**
```
💰 Going cheap — 2018 Toyota Tacoma TRD Off-Road
Auction closes in 45 minutes · McDougall Auctioneers, Saskatoon, SK
Current bid: $14,500  →  All-in: $17,400
Estimated value: $24,000  ·  Margin at current bid: $6,600
Confidence: high (18 comps) · Condition: good
✅ Recent timing chain, single owner, dealer-maintained
[ View lot ]  [ 👍 Interested ]  [ 🤔 Maybe ]  [ 👎 Not interested ]
```

`suspicious_underprice` listings get a leading `⚠ PRICED BELOW TYPICAL LOW END` line.

### 6c. 3-second ACK rule

Every interaction handler calls `await interaction.response.defer(ephemeral=True)` immediately, then DB work, then `await interaction.followup.send(...)`.

### 6d. Rate-limit awareness

Per-channel 5 msgs / 5 sec is the live constraint. Burst notifications get serialized through a single async queue per channel.

### 6e. Quiet hours

22:00–08:00 local: batch most notifications, deliver 08:00 morning digest. **Exception:** `#early-warning` and `#auction-closing` (T−1h) fire immediately because lead time and urgency override quiet hours.

### 6f. User actions

| Button | DB effect | Re-notify? |
|---|---|---|
| 👍 Interested | `user_action='interested'`, pinned in dashboard | All triggers (early-warning, going-cheap, closing-soon, bid-trajectory, extension) |
| 🤔 Maybe | `user_action='maybe'`, kept in dashboard | Going-cheap and closing-soon only |
| 👎 Not interested | `user_action='not_interested'`, hidden | Suppressed |
| Bought it | (dashboard only) `was_purchased_by_us=true` | Done with lot |

### 6g. Curbsider warning

Notifier checks YTD `purchases` count before each push. At count ≥3, every embed adds a footer reminder. At count =4, going-cheap notifications for lots in non-home provinces route to `#system-health` instead of pushing — one safety vehicle below BC's hard 5-vehicle deeming clause.

---

## 7. Dashboard

FastAPI + Jinja2 + HTMX, `localhost:8000`. No auth (single user, local). Mobile-friendly. Auth seam in place (`current_user` dep returns stub) for cloud later.

### 7a. Views

- **Auction feed (default landing)** — lot cards sorted by combined `rarity_score × 0.5 + price_deal_score × 0.5` (configurable). Filters: provinces, year range, make/model multiselect, score thresholds, condition floor, "exclude not-interested", auction-subtype, time-to-close. Each card shows fair-value range as a horizontal bar with marker for current all-in cost, plus a rarity badge if applicable.
- **Closing soon** — lots closing in next N hours (default 24), sorted by close time. Live-updating bid display via HTMX polling.
- **Watched lots (worklist)** — lots flagged `interested` or `maybe`. Notes per lot. Quick actions: change tier, mark bought, add note. Sub-tabs for `interested` vs `maybe`.
- **Lot detail with comp comparison** — for any lot, side-by-side panel showing matching vehicles:
  - From `historical_sales` (sold comps within match criteria), most recent first
  - From open `auction_lots` (currently up for auction), sorted by close time
  - Each comp displays photo, current/final price (channel-normalized), location, end date if open, link to its detail page
  - Match criteria: same year ±2, same make, model, mileage ±20%; widens to ±3 years if no exact matches
  - Shown as horizontally-scrolling card row above the lot's own data
- **Comp browser** — pick year+make+model+trim → see all comps used by valuator, histogram, P10/P50/P90 markers.
- **Sold-price browser** — `historical_sales` view, distribution by month, cross-platform comparison (auction estate vs commercial vs phase-2 listings).
- **Purchases / legal** — transfers table, YTD count with curbsider threshold gauge.
- **System health** — workers, queue backlogs, LLM cost, Discord state.

### 7b. HTMX patterns

Same-URL-two-responses (HX-Request dependency), cursor pagination via `hx-trigger="revealed"`, OOB swaps for cross-region updates, `hx-push-url="true"` on filter forms, charts via Chart.js with inline `<script>` re-running on swap.

### 7c. Action endpoints

| Endpoint | Purpose |
|---|---|
| `POST /lots/:id/mark` | Set `user_action` (interested / maybe / not_interested) |
| `POST /lots/:id/notes` | Append a note |
| `GET /lots/:id/comps` | Fetch the comp comparison panel (HTML fragment) |
| `POST /lots/:id/refresh_bid` | Force a one-shot bid poll (manual override) |
| `POST /admin/rescore` | Trigger valuator backfill |
| `POST /purchases` | Record a buy |
| `PATCH /purchases/:id` | Record sale outcome |

Discord button webhooks call these same endpoints.

---

## 8. Tech stack

### Language and toolchain

- **Python 3.12+**
- **uv** for packaging and venv
- **Ruff** for lint and format
- **Pyright** in strict mode
- **pytest + pytest-asyncio**
- **pydantic-settings + .env** for configuration

### Stack

| Concern | Choice |
|---|---|
| ORM | SQLAlchemy 2.0 async |
| Migrations | Alembic |
| Postgres driver | psycopg 3 (async) |
| HTTP client | httpx; `curl_cffi` reserved for if/when TLS-fingerprinting becomes necessary |
| HTML / JSON parsing | selectolax + `json.loads` for embedded JSON blobs (HiBid `lotModels`, similar) |
| Browser automation (only if needed) | Playwright |
| Web framework | FastAPI |
| Templating | Jinja2 |
| Frontend | HTMX 2.x (vendored), Chart.js 4.x |
| Discord bot | discord.py v2.x |
| LLM | openai SDK (MVP); abstraction wraps it. Anthropic and Ollama as alternative providers. |
| Pydantic ↔ structured output | `client.beta.chat.completions.parse(response_format=Model)` |
| Phone normalization | `phonenumbers` |
| Image dedup | `imagededup` (phase 2) |
| Scheduling | systemd timers (periodic) + systemd services (continuous); no APScheduler |
| Optional queue framework | `procrastinate` (Postgres-backed) considered if hand-rolled queue gets unwieldy |
| Logging | structlog → JSON → journalctl |

### Repo layout

```
CarBuyerAssistant/
├── src/carbuyer/
│   ├── apps/
│   │   ├── auction_discoverer/
│   │   ├── lot_scraper/
│   │   ├── enricher/
│   │   ├── valuator/
│   │   ├── bid_poller/
│   │   ├── vision_batcher/
│   │   ├── auction_distiller/
│   │   ├── notifier/
│   │   ├── dashboard/         # FastAPI + Jinja2 + HTMX
│   │   └── bot/               # discord.py
│   ├── db/
│   ├── llm/
│   │   ├── base.py            # LLMProvider ABC
│   │   ├── openai.py          # MVP
│   │   ├── anthropic.py
│   │   └── ollama.py          # phase 2
│   ├── sources/
│   │   ├── base.py            # Source ABC, AuctionSource, ListingSource
│   │   ├── hibid/             # HiBid platform (multi-auctioneer)
│   │   ├── mcdougall/         # McDougall in-house
│   │   ├── ritchiebros/       # Ritchie Bros / IronPlanet
│   │   ├── michener_allen/    # Michener Allen Edmonton
│   │   └── farmauctionguide/  # Discovery aggregator
│   ├── scoring/
│   ├── transport/             # transport + inspection cost model
│   ├── legal/                 # YTD transfer tracking
│   ├── flags/                 # red/green taxonomy + model-specific gotchas + desirability taxonomy
│   └── shared/
├── tests/
├── alembic/
├── docs/specs/
├── infra/
│   ├── docker-compose.yml     # local Postgres
│   └── systemd/               # one unit per worker
├── pyproject.toml
├── uv.lock
└── README.md
```

The `sources/` package follows a plugin pattern (inspired by stephanlensky/hyacinth). Each `AuctionSource` implements `discover_auctions / fetch_auction / fetch_lots / fetch_lot / poll_bid`. Each plugin owns its source-specific parsing (HiBid `lotModels` JSON, McDougall HTML structure, Ritchie Bros catalog format, etc.) and exposes a normalized output to downstream stages.

---

## 9. Phase-2 work

| Phase-2 item | What MVP reserves |
|---|---|
| Listing comp augmentation (Kijiji, AutoTrader.ca) | `sources/` accommodates `ListingSource` subclass; channel multipliers (private × 1.00, dealer × 0.92) ready in §4b |
| Government surplus auctions (govdeals.ca, BC Auctions, Alberta surplus) | `auction_subtype='govt'` already in enum; `auction_govt × 1.15` channel multiplier reserved |
| IAA / Copart salvage auctions | Different scoring entirely; deferred indefinitely |
| Multi-search profiles | `searches` table exists; UI hardcodes a single row in MVP |
| Family/friends multi-user | `searches.user_id` exists, hardcoded; auth middleware single addition |
| Cloud migration | Postgres portable; only LLM provider configuration changes |
| Classics deeper sub-product (BaT-style auctions, Hagerty integration) | Rarity scoring covers 80% of the use case in MVP; deeper specialty deferred |
| Calibration pipeline | `scoring_version` + `historical_sales` ready as inputs |
| VIN normalization (NHTSA vPIC) | Slot in before LLM in description-enricher |
| Paid valuation API (Vincario, ~€2/lookup) | Provider-swappable client design accommodates |
| Carfax dealer subscription | Same swappability; Carfax extractor is a single function |
| Reverse-image search verification | Add as a verification call for high-rarity / high-deal lots |
| Phone-cross-listing search (curbsider detection) | Once listings come back as comps in phase 2 |
| FB Marketplace | If ever needed: pay Apify ($0.01/listing) on demand, not built |
| Photo bytes archive | Optional table for purchased vehicles only |

---

## 10. Open questions

These are deferred to plan-writing or implementation:

- **Ritchie Bros and Michener Allen scraping feasibility** — HiBid + McDougall + farmauctionguide are confirmed scrapable from prior research. RB and Michener Allen are planned phase-1 sources but their platforms haven't been verified against. Validate before committing to them in the implementation plan.
- **Comp-comparison match criteria for rare/classic vehicles** where exact comps don't exist. Default match (same make/model/trim, year ±2, mileage ±20%) will yield zero comps for a 1985 Land Cruiser FJ60 in our database. Fallback: broaden to make-and-class (`Land Cruiser`-family / `solid axle SUV`-class) with a "fuzzy comp" badge. May involve LLM judgment for class assignment.
- **Desirability taxonomy initial population.** The `desirable_trim_or_spec` and `classic_or_collector` taxonomies need a starting list. Source: model-specific forums, BaT auction history, Hagerty's collector index, our own iterative tagging. Initial list at plan-write time; refined post-launch.
- **Final decision on Postgres queue** (roll our own NOTIFY + SKIP LOCKED, or adopt Procrastinate). Default: roll our own for MVP, revisit if it gets unwieldy.
- **Postgres backup strategy** — `pg_dump` cron + offsite copy; concrete tool TBD.
- **Bid sniping disclosure.** We're observing only — never bidding. Document this clearly so usage stays clean.
- **Buyer's premium variance per platform.** Most are 5–15%; some specialty/online-only fees stack. Auctioneer rules can be PDF-only — extracting BP reliably might require LLM fallback when the structured field is missing.

### Assumptions worth flagging

- **The legal-tracking feature relies on the user manually recording each purchase and sale via the dashboard `purchases` view.** If the user forgets to record a transaction, the YTD count is wrong. The dashboard surfaces a "did you record this?" prompt for any lot marked `was_purchased_by_us` that lacks a corresponding `purchases` row.
- **Disposition labeling at distillation is best-effort.** Lots that don't sell are `unsold`; lots withdrawn for other reasons are `cancelled`. A single closed lot might be labeled imperfectly; downstream comp use accommodates.
- **Carfax extraction depends on the seller including a usable link in the lot description.** When sellers paste a screenshot or describe the report in prose, our extractor will miss most of it — degrade gracefully.
- **Soft-close polling cost.** A 30s poll cadence on the last 10 min of a lot is fine for one lot. At 50 lots closing in the same hour, cap concurrent fast-pollers at ~20 and slow the rest to 60s.

---

## 11. Research-backed decisions

Key research findings that drove non-obvious design choices:

- **Auction-only MVP, listings deferred:** Kijiji's deal density has dropped dramatically (the canonical free Kijiji scrapers on GitHub are abandoned because they all chose CSS selectors and Kijiji churned), AutoTrader is dealer-markup at retail, and FB Marketplace is hostile to scraping (the canonical OSS Marketplace scraper archived in Nov 2024). Auctions are where the deals are: small farm/estate auctioneers, less competition, vehicles priced for local farm-buyers not flippers.
- **HiBid as primary substrate for phase 1:** ~47 Western Canadian auctioneers run on HiBid; ~15–25 actively run vehicle inventory. One scraper covers Mack, Schmalz, Terry McDougall, Graham, Penner, Kaye's, plus dozens more. Pages embed `lotModels` JSON in `<script>` tags (same pattern as Kijiji's `__NEXT_DATA__`) — no headless browser needed. Light anti-bot, no Cloudflare/Turnstile.
- **farmauctionguide.com as discovery:** pure event-level aggregator with per-province feeds. Used to discover auctions on platforms HiBid doesn't cover. Not the lot-data source itself.
- **McDougall direct integration:** runs its own non-HiBid platform with a dedicated Vehicles + Vocational Trucks taxonomy. Weekly Regina/Saskatoon/Winnipeg/Aldersyde sales — the highest-cadence source in the lineup.
- **Soft-close polling:** HiBid soft-close extends end times by 2–4 minutes per late bid; the advertised end is a floor. Tiered poll schedule (60min → 15min → 5min → 60s → 30s) and continue past `scheduled_end` until status flips to `closed`.
- **Bid history is reconstructed:** only current bid is public on HiBid; historical bids require login. We poll and persist our own observations to `auction_bid_history`, which gives us trajectory plotting and bid-velocity signals without auth.
- **Two-trigger alert model:** rare/popular needs early discovery-time notification because lead time matters when the auction is far away. Going-cheap needs late notification because the bid is what it is until close. These are separate events on separate channels — never collapsed into a single score.
- **Rarity gated by desirability:** low historical comp count is not by itself a rarity signal — many vehicles have low sales because nobody wanted them. The score combines LLM judgment of desirability (taxonomy-driven) with database signals (comp count, recent appreciation). LLM-undesirable + low-comp = "nobody wanted this, still don't" → not rare.
- **Channel-normalized comps:** a vehicle that auction-cleared at $12k is not a $12k private-sale comp; the auction buyer paid a discount for accepting "as-is, where-is" with no test drive. Multipliers normalize to private-sale-equivalent (auction_estate × 1.20, auction_commercial × 1.10, dealer × 0.92, private × 1.00). MVP defaults; calibrated from data eventually.
- **Pay-as-you-go API over a flat-rate tier:** a metered API on gpt-4o-mini costs ~$12/mo at MVP volume versus ~$100/mo for a flat-rate subscription tier, and the provider abstraction lets us swap providers later.
- **Two-pass vision over single-pass gallery:** VLMs underperform on multi-image reasoning; per-image extraction then JSON-only aggregation matches the strength curve.
- **Legal tracking as first-class feature:** BC has a hard 5-vehicle deeming clause; Alberta has zero tolerance; Ontario raised curbsider minimum fine to $5,000 in Dec 2023. The system needs to know how many transfers I've done this year to keep me legal. Default thresholds: 3 = soft warning footer on every notification, 4 = interprovincial alerts route to `#system-health` instead of pushing.
- **Transport + inspection cost in the score:** a Toronto buyer winning a Calgary lot pays ~$1,750–2,400 transport plus $550 inspection-and-contingency. Materially changes which lots are deals. Critical for rural auction pickups.
- **systemd timers + services over APScheduler/Celery:** 4 of the workers are periodic with no shared state — oneshot timer + service is the cleanest fit. The continuous workers (lot-scraper, enricher, valuator, bid-poller, notifier) are queue listeners — `Restart=always` services. Job queues add infrastructure (Redis/RabbitMQ) without solving a problem we have.
- **Residential home IP is the right MVP:** cloud IPs are ~99% bot traffic by reputation; Mullvad/ProtonVPN are *worse* than home because their IPs are flagged. Stay home until something specific breaks.
- **Native OpenAI structured outputs over `instructor`:** grammar-constrained decoding eliminates the re-ask loop `instructor` exists to provide.

---

End of design.
