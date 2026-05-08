# CarBuyerAssistant — MVP design

**Date:** 2026-05-08
**Status:** Approved (pending final spec review)
**Scope:** First buildable version of a personal Canadian used-vehicle deal-finder.

---

## 1. Goals & non-goals

### Goals (MVP)

- A personal tool that scrapes Kijiji.ca for used-vehicle listings (cars / SUVs / crossovers / trucks, $8–25k) across multiple Canadian provinces.
- Estimate fair value for each listing from a comp set drawn from current and historical listings; surface listings priced meaningfully below adjusted fair value.
- Run an LLM enrichment pass on every listing to extract structured red/green flags from the description, normalize vehicle facts, and assign a 5-bucket condition (`bad / poor / decent / good / great`).
- Run a nightly vision-LLM pass on the price-shortlist (top ~10% by price-deal score) to assess condition from photos and detect contradictions with the description.
- Push deal notifications to Discord (channel-split: hot deals / watchlist / vision updates / system health) with inline action buttons.
- Provide a localhost web dashboard for browsing the full inventory, comp data, sold-price history, and active deals.
- Begin accumulating a sold-price database from day one by tracking listings until they disappear; distill them into a permanent comp store after a 30-day cool-down.
- Track legal flipping volume (transfers per calendar year) with curbsider-threshold warnings.
- Factor transport cost and per-province inspection cost into deal scoring.
- Extract Carfax data opportunistically when sellers link or attach it.

### Non-goals (explicit)

- Multi-user accounts, family/friend wishlists, public signups (designed-for, not built-for).
- Sources other than Kijiji in MVP. AutoTrader.ca is the planned phase-2 source.
- Facebook Marketplace as a build target (use Apify on demand if ever needed).
- EVs and motorcycles.
- Classic cars (separate sub-product later, different valuation model).
- Photo analysis on the firehose — only on the shortlist, only nightly.
- Paid valuation APIs (CBB, Carfax dealer subscription) — design with a slot for them, don't build against them.
- Real-time vision analysis.
- Cross-border (US) flipping.

### Operating constraints

- The system runs on an always-on Linux machine at home. Cloud-portable from day one (env-driven secrets, no localhost-hardcoded URLs, exportable Postgres).
- Residential IP for all scraping. Conservative cadence (Kijiji every 30 min).
- LLM costs targeted under $20/month at MVP volume (500 listings/day, ~30/night vision shortlist).

---

## 2. System architecture

### Shape

Seven decoupled workers + one dashboard + one Discord bot, communicating through a single Postgres database. Workers are independent processes with their own schedules and retry behavior; failure of any one stage does not cascade.

```
                      ┌────────────────┐
                      │   Postgres     │  (source of truth, append-mostly)
                      └────────────────┘
                              ▲ ▼
   ┌────────────┐  ┌────────────────────┐  ┌────────────────────┐
   │ Scraper    │  │ Description        │  │ Valuator           │
   │ (timer)    │→│ enricher (queue)   │→│ (queue)            │
   └────────────┘  └────────────────────┘  └────────────────────┘
   ┌────────────┐  ┌────────────────────┐  ┌────────────────────┐
   │ Sold-price │  │ Vision-batcher     │  │ Notifier           │
   │ poller     │  │ (timer, nightly)   │  │ (queue)            │
   │ (timer)    │  └────────────────────┘  └────────────────────┘
   └────────────┘  ┌────────────────────┐  ┌────────────────────┐
                   │ Distiller          │  │ Dashboard          │
                   │ (timer, nightly)   │  │ + Discord bot      │
                   └────────────────────┘  └────────────────────┘
```

### Worker cadence and execution model

| Worker | Cadence | Execution |
|---|---|---|
| Scraper | every 30 min, jittered | systemd timer + oneshot service |
| Sold-price poller | every 4–6h | systemd timer + oneshot service |
| Vision-batcher | nightly 02:00 | systemd timer + oneshot service |
| Distiller | nightly 03:00 | systemd timer + oneshot service |
| Description enricher | continuous (queue listener) | systemd `Type=simple, Restart=always` |
| Valuator | continuous (queue listener) | systemd `Type=simple, Restart=always` |
| Notifier | continuous (queue listener) | systemd `Type=simple, Restart=always` |
| Dashboard | continuous | systemd `Type=simple, Restart=always` |
| Discord bot | continuous | systemd `Type=simple, Restart=always` |

All units depend on the local Postgres unit and journal their output; `journalctl -u <name>` is the canonical log path.

### Inter-worker coordination

- Queue listeners watch for `*_status='pending'` rows. The trigger is a Postgres `LISTEN/NOTIFY` channel signaled by the upstream worker on insert/update; the listener wakes, claims work via `SELECT … FOR UPDATE SKIP LOCKED`, processes, commits, and goes back to waiting.
- A dedicated psycopg3 `AsyncConnection` (autocommit, separate from the SQLAlchemy pool) handles `LISTEN`. The SA pool handles all other queries.
- For singleton operations (e.g. dashboard-triggered "rescore everything") use `pg_advisory_xact_lock(hashtext(...))` to serialize across workers.

### Data flow (one listing's lifecycle)

1. Scraper sees a Kijiji listing, parses its `__NEXT_DATA__` JSON blob, inserts a row with `enrichment_status='pending'` and emits `NOTIFY enrichment_pending`.
2. Description enricher claims the row, calls the LLM (description pass), writes structured output (normalized vehicle facts, condition, flags, optional Carfax findings), sets `enrichment_status='done'`, `valuation_status='pending'`, and emits `NOTIFY valuation_pending`.
3. Valuator claims the row, builds the comp set, computes the fair-value range and price-deal score, sets `valuation_status='done'`. If the score crosses a notification threshold and no showstopper flag is set, emits `NOTIFY notification_pending`.
4. Notifier claims the row, posts to the appropriate Discord channel, sets `notification_status='sent'`, records `notified_at` and `notified_channel`.
5. Vision-batcher (nightly) takes the day's price-shortlist (top ~10%), runs the two-pass vision LLM, writes vision findings; if findings contradict description condition by ≥2 buckets, emits a follow-up Discord message and updates the score.
6. Sold-price poller re-checks each `disposition='active'` row periodically; when a listing disappears, sets `disposition='disappeared'` and `disappeared_at`.
7. Distiller (nightly) finds rows that have been `disappeared` for 30+ days, copies distilled fields into `historical_sales`, and deletes the `active_listings` row — except rows where `was_purchased_by_us=true` (kept forever) or `user_action='interested'/'maybe'` (kept 90+ days).

---

## 3. Data model & retention

### `active_listings` — full fidelity, working set

- Identifying: `id`, `source_platform` (`kijiji` / `autotrader` / `fb_marketplace` / etc.), `source_listing_id`, `url`, `title`, `description`, `asking_price_cad`, `posted_at`, `seen_first_at`, `seen_last_at`.
- Photos: array of URLs only (never bytes).
- Seller: `sale_channel` (`private` / `dealer` / `unknown`), `seller_province`, `seller_city`, `seller_phone_normalized` (E.164 via `phonenumbers`).
- Vehicle facts (post-enrichment): `year`, `make`, `model`, `trim`, `engine`, `transmission`, `drivetrain`, `mileage_km`, `vin` (when present).
- Title status: `title_status` ∈ {`NORMAL`, `SALVAGE`, `REBUILT`, `NON_REPAIRABLE`, `STOLEN`, `UNKNOWN`}, plus `province_of_origin` for cross-province brand reconciliation.
- LLM enrichment (description): `condition_categorical`, `condition_confidence`, `red_flags` (jsonb), `green_flags` (jsonb), `showstopper_flags` (jsonb), `carfax_url`, `carfax_findings` (jsonb), `summary`, `enrichment_version`.
- LLM enrichment (vision, when run): `vision_findings` (jsonb), `vision_condition_overall`, `vision_confidence`, `vision_contradictions`.
- Valuation: `comp_count`, `comp_value_low_cad`, `comp_value_mid_cad`, `comp_value_high_cad`, `expected_value_cad`, `landed_cost_premium_cad`, `price_deal_score`, `flag_score`, `confidence_bucket`, `suspicious_underprice_flag`, `scoring_version`, `weights_hash`.
- Lifecycle: `disposition` ∈ {`active`, `disappeared`, `dispositioned`}, `disposition_reason` ∈ {`sold`, `withdrawn`, `expired`, `unknown`}, `disappeared_at`.
- Worker statuses: `enrichment_status`, `valuation_status`, `vision_status`, `notification_status` — each `pending` / `done` / `failed` / `skipped`.
- User action: `user_action` ∈ {`null`, `interested`, `maybe`, `passed`}, `notified_at`, `notified_channel`, `was_purchased_by_us`, `notes`.

### `historical_sales` — distilled, append-only

Schema-versioned. Distillation copies these fields and discards the rest:

- Vehicle: `year`, `make`, `model`, `trim`, `engine`, `transmission`, `drivetrain`, `mileage_km`, `title_status`, `province_of_origin`.
- `condition_categorical`.
- `final_listed_price_cad`, `days_listed`.
- `sale_channel` ∈ {`private`, `dealer`, `auction_estate`, `auction_govt`, `auction_commercial`, `auction_salvage`, `other`}.
- `sale_platform` (string, indexed) — specific source: `kijiji`, `autotrader`, `fb_marketplace`, `hibid`, `mcdougall`, `ritchie_bros`, `govdeals_ca`, `mack_inhouse`, `schmalz_inhouse`, etc.
- For auctions: `buyer_premium_pct_at_sale`, `final_price_with_premium_cad` (the actual all-in hammer-plus-premium).
- `seller_province`, `seller_city`.
- `observed_first_at`, `disappeared_at`.
- `disposition_reason`.
- Score-feedback: `was_notified` (bool), `was_purchased_by_us` (bool), `notes`.
- `schema_version`.

Disposition labeling at distillation time is heuristic:
- Price held steady → `sold`.
- Price kept dropping then vanished → `sold` (lower number).
- Same VIN/photos reposted by same seller within 7 days → `withdrawn`.
- Hit Kijiji's 60-day expiry without price changes → `expired`.

### `purchases` — vehicles I actually buy (legal-tracking)

- `purchase_date`, `sale_date` (nullable while held), `make`, `model`, `year`, `purchase_price_cad`, `sale_price_cad`, `province_of_purchase`, `province_of_sale`, `transport_cost_cad`, `inspection_cost_cad`, `repair_cost_cad`, `notes`, `linked_listing_id`.
- Used by the dashboard to compute YTD transfer count and warn when approaching curbsider-license thresholds.

### `searches` — reserved for phase 2

Single MVP row representing the active filter (year range, make/model whitelist, province set, score threshold, etc.). Phase 2 expands to multiple searches per user; `user_id` column exists from day one but defaults to `me`.

### Indexes (initial)

- `active_listings (make, model, year)` — comp lookups.
- `active_listings (price_deal_score DESC, seen_first_at DESC)` — dashboard deal feed.
- `active_listings (seller_phone_normalized, year, make, model)` — dedup.
- `active_listings (enrichment_status, valuation_status, vision_status, notification_status)` — partial indexes on `pending` for queue claims.
- `historical_sales (make, model, year, mileage_km)` — comp lookups.

### Retention rules

- `active_listings` rows distill into `historical_sales` 30 days after `disappeared_at`, **except**:
  - `was_purchased_by_us=true` → kept forever.
  - `user_action ∈ ('interested', 'maybe')` → kept 90+ days, then distilled with the user_action retained.
- Photo URLs in `active_listings` go stale when the listing disappears; never re-fetched. We never store photo bytes.

---

## 4. Scoring model

### 4a. Comp set

For a target listing:
- Same `make` + `model` + `trim`.
- `year` within ±1.
- `mileage_km` within ±20%.
- Same province preferred; widen to all-of-Canada if fewer than 5 comps.
- Drop `active_listings` comps with `days_listed > 21` (stale-listing bias correction).
- Drop comps with `mileage_km` z-score outside ±2 within the comp set.

Pull from `historical_sales` first (true sold-price proxies); top up from `active_listings` if needed.

Trims apply *before* the confidence check below. If fewer than 5 comps survive trimming, the listing is `confidence_bucket='insufficient'`, gets no `price_deal_score`, and is surfaced in the dashboard as "uncomped — review manually" but never auto-notified.

### 4b. Channel normalization (private-sale-equivalent)

Every comp price is normalized to a private-sale-equivalent before the percentile computation. The reasoning: a vehicle that auction-cleared for $12k is not a $12k private-sale comp — the auction buyer paid a discount for accepting "as-is, where-is" with no test drive, no recourse, and rural pickup. The private-sale equivalent is meaningfully higher. We need all comps in the same units before computing P10/P50/P90.

Channel multipliers (applied to each comp's price; calibrated empirically once we have enough cross-channel observations):

| Source channel | Multiplier to convert to private-equivalent | Rationale |
|---|---|---|
| `private` | 1.00 | reference |
| `dealer` | 0.92 | dealer prices average 6–10% above private for the same vehicle (markup) |
| `auction_estate` | 1.20 | auction buyers discount for no-test-drive, as-is, rural pickup |
| `auction_govt` | 1.15 | govt vehicles are better-documented, discount is smaller |
| `auction_commercial` | 1.10 | well-documented commercial inventory, smaller discount |
| `auction_salvage` | excluded | different scoring entirely |

Numbers are MVP defaults — research-supported but rough. Calibration phase 2 learns them from data: for any pair of comps that match on `(year, make, model, trim, mileage ±10%)` but differ in `sale_channel`, the price ratio is a sample of the true conversion factor. Group by channel pair, take median, store as live config.

`expected_value` (§4c) is therefore always *what this vehicle would clear at in a private sale*. This is the right reference because flipping math assumes the user resells privately — auction buy-back-out-as-private is the canonical flip.

For valuing an auction lot specifically, this means the comp set is normalized to private-equivalent (just like a listing), then auction-specific scoring (§9.1.5) backs out the `recommended_max_bid` from that private-equivalent expected value, accounting for buyer's premium, tax, and landed cost.

### 4c. Asking-to-sold haircut

Applied per-comp from `active_listings`:
- `0.95` if `days_listed < 15`
- `0.93` if `15 ≤ days_listed ≤ 45`
- `0.89` if `days_listed > 45`

`historical_sales` comps are weighted by `disposition_reason`:
- `sold` — `final_listed_price` used directly, no haircut, weight 1.0.
- `withdrawn` — final price likely never cleared on this platform; weight 0.3, no haircut.
- `expired` — listing didn't sell at that price in 60 days, strong overpriced signal; weight 0.2 with an additional 5% haircut.
- `unknown` — weight 0.5, default haircut applied.

The percentile computation weights each comp accordingly (weighted P10/P50/P90).

### 4d. Fair-value range

From the comp set:
- `value_low = P10`, `value_mid = P50`, `value_high = P90`.

Map this listing's LLM-assessed condition to a position in the range:

| Condition | Position |
|---|---|
| bad | 0% (`value_low`) |
| poor | 25% |
| decent | 50% (`value_mid`) |
| good | 75% |
| great | 100% (`value_high`) |

`expected_value = value_low + position × (value_high - value_low)`.

### 4e. Landed cost premium

For each listing in a non-home province:
```
transport_estimate = max(600, 400 + 0.65 × distance_km)
inspection_cost_by_dest = {AB: 200, ON: 120, BC: 125, QC: 125, MB: 100, SK: 75, NS: 50, NB: 50, others: 75}
repair_contingency_by_dest = {AB: 350, ON: 350, QC: 250, others: 150}
landed_cost_premium = transport_estimate + inspection_cost_by_dest[dest] + repair_contingency_by_dest[dest]
```

Same-province listings have `landed_cost_premium = 0`.

### 4f. Price-deal score

```
price_deal_score = (expected_value - asking_price - landed_cost_premium) / expected_value
```

Positive = underpriced after landed costs. The threshold for notification is 0.15 (≥15% below adjusted fair value, after transport).

`suspicious_underprice` flag fires (does not block notification, just labels) when `asking_price < value_low × 0.85`.

### 4g. Flag score

LLM emits a list of red flags and green flags, each with a category and weight. `flag_score = clamp(sum(weights), -5, +5)`. Examples:

| Flag | Weight |
|---|---|
| Engine knock / seized / overheat | -3 |
| Frame damage / structural | **showstopper** |
| Salvage title (not rebuilt) | **showstopper** |
| "As-is" / "for parts" / "won't start" | **showstopper** |
| Accident on Carfax | -2 |
| "Needs work" / "project" | -2 |
| No service records | -1 |
| Recent timing belt/chain | +1 |
| Recent transmission service | +1 |
| No accidents on Carfax | +2 |
| Single owner | +1 |
| Service records / dealer-maintained | +2 |

Showstoppers exclude the listing from notifications regardless of `price_deal_score`.

### 4h. Notification rule

For a hot-deal push notification:
- No showstopper flag.
- `confidence_bucket ∈ {medium, high}` (≥5 comps).
- `price_deal_score ≥ 0.15` **AND** `flag_score ≥ -1`.
- Vehicle is in the active search filter.
- Not already notified (dedup by listing ID; re-notifies on price drops if score improves by ≥0.05; re-notifies on `Maybe` user_action; suppressed on `Passed`).
- Not in quiet hours (22:00–08:00 local), unless `price_deal_score ≥ 0.30`.

For dashboard "watchlist":
- `price_deal_score ≥ 0.08` **OR** `flag_score ≥ +2`.

### 4i. Score versioning

Every scored row records `scoring_version` and `weights_hash`. Weight changes don't retroactively rewrite history; backfill is an explicit admin action. Lets us A/B-test future weight changes against `historical_sales` outcomes.

### 4j. Calibration (phase 2)

When `historical_sales` accumulates enough data:
- Replace P10/P90 spread with median-price-per-condition-bucket curve, learned per make/model.
- Calibrate the asking-to-sold haircut from real listing-to-disappearance pairs.
- Calibrate flag weights from cross-correlation with `days_listed` and `disposition_reason`.
- **Calibrate channel multipliers**: for any pair of `historical_sales` rows that match on `(year, make, model, trim, mileage_km ± 10%)` but differ in `sale_channel`, the price ratio is one observation of the true conversion factor. Group by channel pair, take median, store as live config. Falls back to MVP defaults (1.20 / 1.15 / 1.10 / 0.92) when sample size is insufficient.

---

## 5. LLM enrichment

### 5a. Provider abstraction

```
LLMProvider (ABC)
  describe(listing_input) -> EnrichmentOutput
  vision(photos, context) -> VisionOutput

OpenAIProvider          # MVP — gpt-4o-mini for both passes
AnthropicProvider       # alternative
OllamaProvider          # local, phase 2
```

Provider selected per stage from config. Cost ceiling enforced inside each provider; rate-limit errors trigger configurable fallback to the next provider in the chain.

### 5b. Description pass (real-time, every listing)

**Inputs:** title, description, structured scraper fields, Carfax URL if extractable from description text, plus a static knowledge block injected into the prompt:
- Canonical taxonomy of red/green flags (from §4g).
- Model-specific gotchas table (Tacoma frame rust 2005–2015, CR-V 1.5T fuel-in-oil 2017–2022, Ford 3.5L EcoBoost cam phaser, Subaru EJ25 head gaskets 1999–2011, Hyundai/Kia Theta II rod bearings, Nissan CVT issues, etc.) — keyed by make/model/year so the LLM only sees relevant entries.
- Scam-pattern catalog (curbsider phone signals, urgency phrasing, "ran when parked", VIN refusal, etc.).

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
    vin: str | None  # only when present in description; we never invent

class FlagInstance(BaseModel):
    flag: str
    evidence: str   # verbatim quote from the listing
    weight: int

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
    seller_type_guess: Literal["private", "dealer", "unknown"]
    summary: str
```

**Prompt rules:**
- "Output `unknown` if you cannot determine the field. Do not guess."
- "If `condition_confidence < 0.5`, output `condition_categorical = decent`."
- "Quote evidence verbatim. Do not paraphrase."
- "Use only flags from the provided taxonomy. Do not invent new ones."

**Carfax extraction:** if `carfax_url` is set, the enricher fetches the page (httpx, no JS) and runs a second small LLM call over its text content, producing:

```python
class CarfaxFindings(BaseModel):
    accident_count: int
    accident_severity_max: Literal["minor", "moderate", "severe", "none"]
    service_record_density: Literal["none", "sparse", "regular", "dense"]
    ownership_count: int | None
    title_brands: list[str]
    odometer_consistency: Literal["consistent", "rollback_suspected", "unknown"]
```

Findings convert to additional flags before scoring.

### 5c. Vision pass (nightly, shortlist only)

Two-pass pattern (research-driven — VLMs underperform on multi-image reasoning):

**Pass 1 — per image:**
```python
class PerImageFinding(BaseModel):
    type: Literal["rust", "dent", "scratch", "paint_mismatch", "panel_gap", "interior_wear", "stain", "other"]
    location: str
    severity: int = Field(ge=1, le=3)
    confidence: int = Field(ge=1, le=5)
    reasoning: str

class PerImageOutput(BaseModel):
    shot_type: Literal["exterior_front", "exterior_rear", "exterior_side", "interior", "engine_bay", "wheel", "undercarriage", "document", "other"]
    image_quality_sharpness: Literal["sharp", "blurry"]
    image_quality_lighting: Literal["well_lit", "dim", "harsh_shadow"]
    image_quality_cleanliness: Literal["clean", "dirty"]
    visible_panels: list[str]
    findings: list[PerImageFinding]
    explicit_unknowns: list[str]
```

**Pass 2 — gallery aggregation, given Pass-1 JSON only (no images):**
```python
class VisionOutput(BaseModel):
    coverage_gaps: list[str]
    cross_panel_paint_consistency: Literal["consistent", "inconsistent", "cannot_assess"]
    staging_signals: list[str]
    overall_red_flags: list[str]
    overall_green_flags: list[str]
    exterior_condition: Literal["bad", "poor", "decent", "good", "great"]
    interior_condition: Literal["bad", "poor", "decent", "good", "great"]
    overall_vision_condition: Literal["bad", "poor", "decent", "good", "great"]
    vision_confidence: float
    contradictions_with_description: list[str]
```

**Reconciliation rule:** when `vision_confidence > 0.7` and `vision_condition` differs from `description_condition` by ≥2 buckets, the scorer uses `min(description_condition, vision_condition)` (pessimistic) and adds a `description_oversells_condition` red flag (-2).

**Photo prep:** download photos fresh, resize to 1024px long edge JPEG, cap at 8 photos per listing for cost control.

**Cost budget:** nightly cap of 100 listings vision-processed. Excess deferred to next night.

### 5d. Re-enrichment

- Description pass re-runs if the listing's description text changes or asking price drops (price drops sometimes flush new flags into edited descriptions).
- Vision pass does not re-run unless the photo URL set hash changes.

---

## 6. Notifications

### 6a. Channel: Discord bot, channel-split

- `#hot-deals` — `price_deal_score ≥ 0.20`.
- `#watchlist` — meets standard threshold but not extraordinary.
- `#vision-updates` — overnight vision contradictions / confirmations.
- `#system-health` — scraper failures, rate-limit warnings, distillation events, weekly transfer-count check-ins.

Bot connects out to Discord via TCP 443 (no inbound, no port forwarding). Uses `discord.py v2.x`. Slash commands only; intents minimized.

### 6b. Message format

Embedded card with thumbnail, title, asking price, fair-value range bar, score breakdown, top flags, location, age. Inline action row with buttons:

- 👍 Interested — pin in dashboard, future re-notifications continue.
- 🤔 Maybe — kept in dashboard, re-notifies on price drops.
- 👎 Pass — demoted, suppresses future re-notifications on this listing.
- "Bought it" — done from the dashboard, not Discord (sets `was_purchased_by_us=true`, never auto-distilled).

`suspicious_underprice` listings get a leading `⚠ PRICED BELOW TYPICAL LOW END` line in the embed.

### 6c. Persistent views

Buttons survive bot restarts via `discord.ui.DynamicItem` with regex `custom_id` template (`deal:{action}:{listing_id}`). Re-registered in `setup_hook` on every boot.

### 6d. 3-second ACK rule

Every interaction handler calls `await interaction.response.defer(ephemeral=True)` immediately, then does any DB work, then `await interaction.followup.send(...)`.

### 6e. Rate-limit awareness

50 messages/sec global is irrelevant; per-channel 5 msgs / 5 sec is the live constraint. Burst notifications get serialized through a single async queue per channel.

### 6f. Quiet hours and batching

22:00–08:00 local are quiet; notifications queue and deliver as a single batched message at 08:00, sorted by score. Override: `price_deal_score ≥ 0.30` bypasses quiet hours immediately.

### 6g. Curbsider warning

Notifier checks YTD `purchases` count before each push; at count ≥3 the embed includes a footer reminder. At count =4, the system stops auto-notifying about deals where `seller_province ≠ home_province` (transport plus a likely interprovincial transfer raises legal exposure) and routes those to `#system-health` with an explicit notice.

---

## 7. Dashboard

FastAPI + Jinja2 + HTMX, served on `localhost:8000`. No auth (single user, local). Mobile-friendly. Auth seam in place (`current_user` dep returns stub) for cloud later.

### 7a. Views

- **Deal feed** — cards, newest deal first, infinite scroll via `hx-trigger="revealed"` cursor pagination. Filters: provinces, year range, make/model multiselect, score-threshold slider, condition floor, exclude-passed toggle, include-suspicious toggle. Each card shows fair-value range as a horizontal bar with marker for asking price.
- **Active deals (worklist)** — listings with `user_action ∈ {interested, maybe}`. Notes field per listing. Quick actions: Mark Bought / Pass / add note.
- **Comp browser** — pick year + make + model + trim → see comps used by the system. Histogram (Chart.js) of asking prices, markers for P10/P50/P90.
- **Sold-price browser** — `historical_sales` view. Histogram and price-by-month chart.
- **System health** — worker last-run, success/failure stats, queue backlog, LLM cost-to-date, database size, Discord bot connection state.
- **Purchases / legal** — `purchases` table view, YTD transfer count with curbsider threshold gauge.

### 7b. HTMX patterns

- One URL serves both full pages (direct nav) and HTML fragments (HX-Request header) via an `is_htmx` dependency.
- Filter form drives a single target; `hx-push-url="true"` keeps address bar in sync.
- OOB swaps for cross-region updates (filter triggers list update + count badge + histogram in one response).
- Cursor pagination, not offset.
- Charts render container server-side (`<canvas data-points='...'>`), hydrate client-side via inline `<script>` that runs on swap.

### 7c. Action endpoints

- `POST /listings/:id/mark` — set `user_action`.
- `POST /listings/:id/notes` — append a note.
- `POST /listings/:id/snooze` — suppress re-notifications for N days.
- `POST /searches/:id/refresh` — force a scrape now.
- `POST /admin/rescore` — trigger valuator backfill.
- `POST /purchases` — record a buy.
- `PATCH /purchases/:id` — record sale outcome.

The Discord bot's button webhook calls these same endpoints.

---

## 8. Tech stack

### Language and toolchain

- **Python 3.12+**.
- **uv** (Astral) for packaging and virtualenv.
- **Ruff** for lint and format.
- **Pyright** in strict mode for type checking.
- **pytest + pytest-asyncio** for tests.
- **pydantic-settings + .env** for configuration.

### Stack

| Concern | Choice |
|---|---|
| ORM | SQLAlchemy 2.0 async |
| Migrations | Alembic |
| Postgres driver | psycopg 3 (async) |
| HTTP client | httpx; `curl_cffi` reserved for if/when TLS-fingerprinting becomes necessary |
| HTML parsing | selectolax for hot path; BeautifulSoup4 + lxml for ergonomic one-offs |
| JSON extraction | direct `json.loads` of `__NEXT_DATA__` blob from Kijiji pages |
| Browser automation (phase 2) | Playwright |
| Web framework | FastAPI |
| Templating | Jinja2 |
| Frontend | HTMX 2.x (vendored), Chart.js 4.x for visualizations |
| Discord bot | discord.py v2.x |
| LLM | openai SDK (MVP); abstraction wraps it. Anthropic and Ollama as alternative providers. |
| Pydantic ↔ structured output | `client.beta.chat.completions.parse(response_format=Model)` (OpenAI native structured outputs) |
| Phone normalization | `phonenumbers` |
| Image dedup (phase 2) | `imagededup` |
| Scheduling | systemd timers (periodic) + systemd services (continuous); no APScheduler |
| Optional queue framework | `procrastinate` (Postgres-backed) considered for phase 2 if hand-rolled queue becomes unwieldy |
| Logging | structlog → JSON → journalctl |

### Repo layout

```
CarBuyerAssistant/
├── src/carbuyer/
│   ├── apps/
│   │   ├── scraper/             # python -m carbuyer.apps.scraper
│   │   ├── enricher/
│   │   ├── valuator/
│   │   ├── vision_batcher/
│   │   ├── sold_poller/
│   │   ├── notifier/
│   │   ├── distiller/
│   │   ├── dashboard/           # FastAPI + Jinja2 + HTMX
│   │   └── bot/                 # discord.py
│   ├── db/
│   ├── llm/
│   │   ├── base.py              # LLMProvider ABC
│   │   ├── openai.py            # MVP
│   │   ├── anthropic.py
│   │   └── ollama.py            # phase 2
│   ├── sources/
│   │   └── kijiji/              # plugin, parses __NEXT_DATA__
│   ├── scoring/
│   ├── transport/               # transport + inspection cost model
│   ├── legal/                   # YTD transfer tracking
│   ├── flags/                   # taxonomy + model-specific gotchas table
│   └── shared/
├── tests/
├── alembic/
├── docs/specs/                  # this file
├── infra/
│   ├── docker-compose.yml       # local Postgres
│   └── systemd/                 # one unit per worker
├── pyproject.toml
├── uv.lock
└── README.md
```

The `sources/` package follows a plugin pattern (inspired by stephanlensky/hyacinth): each source implements a common `Source` interface so phase-2 AutoTrader.ca slots in without touching downstream stages.

---

## 9. Phase-2 work

| Phase-2 item | What MVP reserves |
|---|---|
| **Auction support (Western Canada)** — see §9.1 for full design | `sale_format` includes `auction`; source plugin interface accommodates auction-shaped sources via `AuctionSource` subclass; `historical_sales` schema unchanged |
| AutoTrader.ca scraper | `sources/` plugin interface; `sale_format` enum value already present |
| Multi-search profiles | `searches` table exists; UI hardcodes a single row in MVP |
| Family/friends multi-user | `searches.user_id` exists, hardcoded; auth middleware is a single addition |
| Cloud migration | Postgres portable; only the LLM provider configuration changes |
| Classics sub-product | Separate scoring path keyed on `is_classic`; same DB |
| Photo bytes archive | Optional table for purchased vehicles only |
| Calibration pipeline | `scoring_version` + `historical_sales` ready as inputs |
| VIN-based normalization | NHTSA vPIC layer, slot in before LLM |
| Paid valuation API | Vincario (~€2/lookup) is the realistic provider; provider-swappable client design accommodates |
| Carfax dealer subscription | Same swappability; Carfax extractor is a single function |
| Reverse-image search verification | Add as a verification call in description-pass for high-deal-score listings |
| Phone-cross-listing search | Same; flags curbsiders with multiple active vehicles |
| FB Marketplace | If ever needed: pay Apify ($0.01/listing) on demand, not built |

---

## 9.1 Phase-2 auction support — locked design

Build order: after Kijiji MVP ships and proves stable. Auctions become the second source family before AutoTrader.ca.

### 9.1.1 Goals

- Discover and track upcoming vehicle auctions across Western Canada (AB / BC / SK / MB).
- Surface lots whose **all-in cost** (current high bid + buyer's premium + tax + landed cost) sits meaningfully below estimated fair value.
- Recommend a max bid that preserves a configurable flip margin.
- Push closing-soon notifications with current bid status and time-remaining urgency.
- Track bid trajectory (we observe; we don't bid).
- Honour HiBid's soft-close mechanic — never assume the nominal end time is final.

### 9.1.2 Sources, prioritised

| Priority | Source | Role | Coverage |
|---|---|---|---|
| 1 | **HiBid** (`hibid.com/{province}/auctions/700006/cars-and-vehicles`) | Primary lot data | ~15–25 active Western CA auctioneers from one scraper |
| 2 | **farmauctionguide.com** (per-province pages) | Discovery feed for auctions not on HiBid | Surfaces auctions on in-house platforms |
| 3 | **McDougall Auctioneers** (in-house at `mcdougallauction.com`) | Direct extractor — has a dedicated Vehicles + Vocational Trucks taxonomy | Weekly Regina/Saskatoon/Winnipeg/Aldersyde sales |
| Reserved | Government surplus (govdeals.ca, BC Auctions, Alberta surplus) | Separate plugin family, deferred | Maintained vehicles with paper trail |
| Reserved | Ritchie Bros and equivalents | Separate plugin, deferred | Larger commercial vehicles, cleaner data |
| Skipped | Proxibid | Heavier anti-bot, thinner Western CA coverage; HiBid already covers the long tail | — |
| Skipped | Salvage auctions (Copart, IAA) | Out of scope — different scoring entirely | — |

### 9.1.3 Source plugin interface

Existing `sources/` package gets a type-discriminated split:

```python
class Source(ABC):
    type: Literal["listing", "auction"]

class ListingSource(Source):
    type = "listing"
    async def discover(self) -> AsyncIterator[ListingRef]: ...
    async def fetch(self, ref: ListingRef) -> RawListing: ...

class AuctionSource(Source):
    type = "auction"
    async def discover_auctions(self) -> AsyncIterator[AuctionRef]: ...
    async def fetch_auction(self, ref: AuctionRef) -> RawAuction: ...
    async def fetch_lots(self, auction_ref: AuctionRef) -> AsyncIterator[LotRef]: ...
    async def fetch_lot(self, ref: LotRef) -> RawLot: ...
    async def poll_bid(self, ref: LotRef) -> BidObservation: ...
```

`KijijiSource` implements `ListingSource`. `HiBidSource`, `FarmAuctionGuideSource` (discovery-only — populates `auctions` rows then defers to a per-auctioneer `AuctionSource` for lots), and `McDougallSource` implement `AuctionSource`.

### 9.1.4 New data model (additive — no MVP table changes)

**`auctions`** — auction events themselves
- `id`, `source` (`hibid` / `mcdougall` / `farmauctionguide_discovered` / etc.), `source_auction_id`, `url`
- `auction_subtype` ∈ {`estate`, `govt`, `commercial`} — drives the channel multiplier when the lot eventually distills. Default `estate` for HiBid + farmauctionguide-discovered + McDougall; `govt` when `source ∈ {govdeals_ca, bc_auctions, alberta_surplus}`; `commercial` for Ritchie Bros and similar. Auctioneer-name regex overrides apply (e.g., `auctioneer_name ~* 'rcmp|police|surplus|government'` → `govt`).
- `auctioneer_name`, `auctioneer_external_id`
- `title`, `description`, `terms_text`
- `scheduled_start_at`, `scheduled_end_at`, `last_seen_end_at`, `closed_at`
- `pickup_address`, `pickup_city`, `pickup_province`, `pickup_window_text`
- `buyer_premium_pct` (decimal), `online_bidding_fee_pct` (some platforms tack on extra)
- `gst_pct`, `pst_pct` (province-derived but per-auction-overridable)
- `status` ∈ {`upcoming`, `live`, `closing`, `closed`, `cancelled`}
- `first_seen_at`, `last_seen_at`, `discovery_confidence` (high if direct HiBid scrape; lower if inferred from farmauctionguide)

**`auction_lots`** — individual vehicles in an auction
- `id`, `auction_id` (FK), `source_lot_id`, `lot_number`, `url`
- `title`, `description`, `photos` (URL array)
- Normalized vehicle facts: `year`, `make`, `model`, `trim`, `engine`, `transmission`, `drivetrain`, `mileage_km`, `vin`, `title_status`, `province_of_origin` (same shape as `active_listings`)
- LLM enrichment (same shape as `active_listings`): `condition_categorical`, `condition_confidence`, `red_flags`, `green_flags`, `showstopper_flags`, `enrichment_status`, `enrichment_version`
- Vision findings (same shape, when run)
- Auction-specific bid state:
  - `current_high_bid_cad` (nullable until first bid)
  - `last_bid_observed_at`
  - `reserve_met` (`null` if no reserve, `true`/`false` if disclosed)
  - `bid_count_visible` (often visible without login)
  - `lot_status` ∈ {`open`, `closing_soon`, `extended`, `closed`, `unsold`, `sold`}
  - `closed_at`, `final_bid_cad`
- Valuation (auction-specific):
  - `comp_count`, `value_low_cad`, `value_mid_cad`, `value_high_cad`, `expected_value_cad`
  - `landed_cost_premium_cad` (transport + inspection + repair contingency, same model as §4e)
  - `all_in_at_current_bid_cad` (current_high_bid × (1+BP) × (1+tax) + landed)
  - `recommended_max_bid_cad` (working backwards from expected_value − configurable margin)
  - `auction_deal_score` (margin-percent at current bid)
  - `scoring_version`
- Lifecycle / user action: `notification_status`, `user_action` (`watching` / `bid_planned` / `passed`), `notes`, `was_purchased_by_us`

**`auction_bid_history`** — what we observe by polling
- `id`, `lot_id` (FK), `observed_at`
- `current_high_bid_cad`, `end_time_at_observation`, `status_at_observation`
- Append-only. Used to compute bid velocity, plot trajectory in the dashboard, and learn typical buyer behavior.

**Indexes:**
- `auctions (status, scheduled_end_at)` — find lots closing soon
- `auction_lots (auction_id, lot_status)`
- `auction_lots (make, model, year)` — comp lookups (auction lots can be comps for listings and vice versa)
- `auction_bid_history (lot_id, observed_at)` — trajectory queries

### 9.1.5 Scoring for auctions

For a given lot at a moment in time:

```
all_in_at_bid = current_high_bid × (1 + buyer_premium_pct) × (1 + gst_pct + pst_pct)
all_in_total  = all_in_at_bid + landed_cost_premium

auction_deal_score = (expected_value - all_in_total) / expected_value
```

`expected_value` is computed exactly as for listings (range-based, condition-mapped — §4d), including the channel-normalization step (§4b) that converts every comp to private-sale-equivalent dollars. The auction comp set draws from both `historical_sales` and *closed* `auction_lots`; once normalized, an auction comp at $12k all-in becomes ~$14.4k private-equivalent and is comparable. Open `auction_lots` are NOT used as comps until they close.

**Recommended max bid** (working backwards from a target margin):

```
flip_margin     = configurable (default $1500 or 10% of expected_value, whichever is greater)
target_all_in   = expected_value - flip_margin
recommended_max_bid = target_all_in / ((1 + buyer_premium_pct) × (1 + gst_pct + pst_pct)) - landed_cost_premium / ((1 + bp) × (1 + tax))
```

Surfaced prominently in the lot detail view: *"Don't bid above $X to keep your $1500 margin."*

**Notification triggers:**
- **Discovery** — fired when a new lot is enriched and `auction_deal_score ≥ 0.15` at current bid (or when current bid is null/no-bid, computed against `recommended_max_bid` as the implicit ceiling).
- **Closing-soon** — fired at `T-24h`, `T-6h`, `T-1h` for any lot the user has marked `watching` or that has an active deal score ≥ threshold. Channel: `#auction-closing`.
- **Bid trajectory alert** — fired when a watched lot's bid crosses 80% of `recommended_max_bid` (decision point for the user).
- **Lot extended** — fired when soft-close pushes a watched lot past its scheduled end time. Channel: `#auction-closing`.

### 9.1.6 New workers

| Worker | Cadence | Notes |
|---|---|---|
| **Auction-discoverer** | 4×/day | Crawls farmauctionguide.com per-province pages + HiBid province search; writes new `auctions` rows |
| **Auction-lot-scraper** | continuous (queue) | For each new `auctions` row, fetches the auction's lot catalog, writes `auction_lots` rows, marks each `enrichment_status='pending'` |
| **Bid-poller** | tiered schedule | Long-running async process. Maintains a priority queue keyed on `next_poll_at`. Tier rules: `>24h to close → 60 min`; `2–24h → 15 min`; `1–2h → 5 min`; `10–60 min → 60 s`; `<10 min or extended → 30 s, continue past scheduled_end until lot_status=closed` |
| **Auction-distiller** | nightly | Closed lots ≥ 14 days past `closed_at` distill into `historical_sales` with `sale_channel = auction_<subtype>` (estate/govt/commercial), `sale_platform = auctions.source` (hibid/mcdougall/etc.), `final_listed_price_cad = final_bid_cad`, `final_price_with_premium_cad = final_bid_cad × (1 + buyer_premium_pct)`, `buyer_premium_pct_at_sale` retained |

The description-enricher and valuator are reused unchanged — they consume `auction_lots` rows the same way they consume `active_listings` rows. The vision-batcher's shortlist query expands to include high-score auction lots.

### 9.1.7 Discord channel additions

- `#hot-deals` (existing) — also receives auction discovery alerts where score ≥ threshold
- `#auction-closing` (new) — closing-soon urgency notifications (T-24h/T-6h/T-1h, extension events, bid-trajectory crosses)
- `#auction-watch` (new) — daily summary of lots the user has marked `watching` with current bid status

### 9.1.8 Auction-specific scam / risk patterns (LLM prompt additions)

- "As-is, where-is" — universal at auction; not itself a flag, but absence of any disclosed condition info IS a flag.
- Vehicles described only as "ran when parked", "hasn't run in years", or with no engine state mentioned.
- No photos of engine bay or under-hood — heavy red flag at auction.
- Lots withdrawn from previous auctions (visible on HiBid via lot history) — repeat sellers signal hidden problems.
- "Reserve not met" status + low bid count — the seller wants more than the market will give; a deal here is unlikely unless the seller capitulates.
- Title-only or no-VIN listings — bid-blind risk.
- Pickup terms with very tight windows (<5 days) — pressure to close transport before issues surface.

### 9.1.9 Open questions (auction-specific)

- **Bid sniping disclosure.** We're observing only — never bidding. The system surfaces information, the user decides. Document this clearly so usage stays clean.
- **Buyer's premium variance.** Most auctions are 5–15%; some specialty/online-only fees stack. Auctioneer rules can be PDF-only — extracting BP reliably might require LLM fallback when the structured field is missing.
- **GST/PST handling.** Provincial tax differences matter when a Saskatchewan buyer wins an Alberta lot. Default to destination-province tax but allow override per-auction.
- **Phone-pseudo-VIN dedup vs auction lots.** Same vehicle re-listed across auctions is rare but possible (estate gets passed to a different auctioneer). Lot-photo perceptual hashing across `auction_lots` and across `auction_lots ↔ active_listings` would catch it. Defer until cross-source dedup proves needed.
- **Soft-close polling cost.** A 30s poll cadence on the last 10 min of a lot is fine for one lot — at 50 lots closing in the same hour it adds up. Cap concurrent fast-poll lots at ~20 and slow the rest to 60s.

---

## 10. Open questions

These are deferred to plan-writing or implementation:

- Final decision on whether to roll our own Postgres queue (NOTIFY + SKIP LOCKED) or adopt Procrastinate. Default: roll our own for MVP, revisit if it gets unwieldy.
- Exact systemd unit dependency graph (postgres → workers).
- Concrete LLM prompt drafts for description and vision passes (built and iterated during implementation).
- Initial scrape filter (year range, vehicle categories, price band) — config-driven.
- Curbsider warning thresholds — currently 3 = soft warning, 4 = hard interprovincial cutoff. Reconcile against legal research per province if user operates outside ON/AB/BC.
- Postgres backup strategy — `pg_dump` cron + offsite copy; concrete tool TBD.

### Assumptions worth flagging

- **The legal-tracking feature relies on the user manually recording each purchase and sale via the dashboard `purchases` view.** If the user forgets to record a transaction, the YTD count is wrong and the curbsider warnings can't fire correctly. The dashboard surfaces a "did you record this?" prompt for any listing marked `was_purchased_by_us` that doesn't have a corresponding `purchases` row.
- **The `disposition_reason` heuristic at distillation time is best-effort.** Some listings labeled `sold` will actually have been withdrawn; some `withdrawn` will have been sold off-platform. The comp-weighting accommodates this uncertainty but doesn't eliminate it.
- **Carfax extraction quality depends on the seller including a usable link.** When the seller pastes a screenshot or describes the report in prose, our extractor will miss most of it — degrade gracefully, don't crash.

---

## 11. Research-backed decisions (reference)

Key research findings that drove non-obvious design choices:

- **Kijiji `__NEXT_DATA__` parsing over CSS selectors**: every actively-maintained free Kijiji scraper on GitHub is broken because they chose selectors. The Next.js hydration JSON is dramatically more stable.
- **OpenAI API over Max plan**: Anthropic's TOS explicitly disclaims SDK use of Max-plan auth; Agent SDK Python/TS don't actually support Max OAuth (issues #559, #11); direct API on gpt-4o-mini costs ~$12/mo at MVP volume vs $100/mo Max plan tier.
- **Two-pass vision over single-pass gallery**: VLMs underperform on multi-image reasoning; per-image extraction then JSON-only aggregation matches the strength curve.
- **Days-of-market-aware asking-to-sold haircut**: iSeeCars 30M-listing study shows ~7% average price-cut; CarGurus shows 60% of 45+ day listings have already cut. Single 0.93 constant is a misfit.
- **AutoTrader.ca over FB Marketplace as source #2**: the canonical OSS Marketplace scraper archived itself in Nov 2024; Meta's anti-bot is a full-time job. AutoTrader.ca is moderately easier than Kijiji with cleaner structured data and ~50–80% VIN coverage on dealer listings.
- **Phone-as-pseudo-VIN dedup**: VIN coverage on Kijiji private listings is ~15–30%; phone normalization (`phonenumbers` E.164) is a stronger signal for the no-VIN majority.
- **systemd timers over APScheduler/Celery**: 4 of 7 workers are periodic with no shared state — oneshot timer + service is the cleanest fit. 3 workers are queue listeners — `Restart=always` services. Job queues add infrastructure (Redis/RabbitMQ) without solving a problem we have.
- **Residential home IP is the right MVP**: cloud IPs are ~99% bot traffic by reputation; Mullvad/ProtonVPN are *worse* than home because their IPs are flagged. Stay home.
- **Native OpenAI structured outputs over `instructor`**: grammar-constrained decoding eliminates the re-ask loop `instructor` exists to provide.
- **Legal tracking as first-class feature**: BC has a hard 5-vehicle deeming clause; Alberta has zero tolerance; Ontario raised curbsider minimum fine to $5,000 in Dec 2023. The system needs to know how many transfers I've done this year to keep me legal.
- **Transport + inspection cost in the score**: a Toronto→Calgary deal carries ~$1,750–2,400 transport plus $550 inspection-and-contingency. This materially changes which listings are deals.
- **Auction phase 2: HiBid as primary substrate**: ~47 Western Canadian auctioneers run on HiBid; ~15–25 of them have active vehicle inventory. One scraper covers Mack, Schmalz, Terry McDougall, Graham, Penner, Kaye's, plus dozens more. Pages embed `lotModels` JSON (same pattern as Kijiji's `__NEXT_DATA__`) — no headless browser needed. McDougall Auctioneers is the one in-house exception worth a direct extractor.
- **Auction phase 2: discovery via farmauctionguide.com**: pure event-level aggregator with per-province feeds. Used to find auctions on platforms HiBid doesn't cover. Not the lot-data source itself.
- **Auction phase 2: soft-close polling**: HiBid soft-close extends end times by 2–4 minutes per late bid. Tiered poll schedule (60min → 15min → 5min → 60s → 30s) and continue past scheduled_end until status flips to `closed`.
- **Auction phase 2: bid history is reconstructed**: only current bid is public; historical bids require login. We poll and persist our own observations to `auction_bid_history`, which gives us trajectory plotting and bid-velocity signals without any auth requirement.
- **Channel-normalized comps**: a vehicle that auction-cleared at $12k is not a $12k private-sale comp; the auction buyer paid a discount for accepting "as-is, where-is" with no test drive. Mixing channels without correction biases fair-value by whatever channel mix happens to be in the comp set. Normalizing every comp to a private-sale-equivalent (default multipliers: auction_estate × 1.20, auction_govt × 1.15, auction_commercial × 1.10, dealer × 0.92) makes the comp set internally consistent and matches the flipping use case where the user resells privately. Multipliers calibrate from same-vehicle cross-channel observations once `historical_sales` is large enough.
- **Two-axis sale taxonomy (channel + platform)**: `sale_channel` captures the type of transaction (`private` / `dealer` / `auction_estate` / `auction_govt` / `auction_commercial`); `sale_platform` captures the specific source (`kijiji` / `autotrader` / `hibid` / `mcdougall` / etc.). Channel drives the normalization multiplier; platform supports source-specific behavior (anti-bot strategies, parsing patterns, dedup keys).

---

End of design.
