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

- Identifying: `id`, `source` (`kijiji`), `source_id` (Kijiji listing ID), `url`, `title`, `description`, `asking_price_cad`, `posted_at`, `seen_first_at`, `seen_last_at`.
- Photos: array of URLs only (never bytes).
- Seller: `seller_type` (`private` / `dealer` / `unknown`), `seller_province`, `seller_city`, `seller_phone_normalized` (E.164 via `phonenumbers`).
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
- `sale_format` ∈ {`kijiji_private`, `kijiji_dealer`, `autotrader`, `fb_marketplace`, `auction`, `other`}.
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

### 4b. Asking-to-sold haircut

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

### 4c. Fair-value range

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

### 4d. Landed cost premium

For each listing in a non-home province:
```
transport_estimate = max(600, 400 + 0.65 × distance_km)
inspection_cost_by_dest = {AB: 200, ON: 120, BC: 125, QC: 125, MB: 100, SK: 75, NS: 50, NB: 50, others: 75}
repair_contingency_by_dest = {AB: 350, ON: 350, QC: 250, others: 150}
landed_cost_premium = transport_estimate + inspection_cost_by_dest[dest] + repair_contingency_by_dest[dest]
```

Same-province listings have `landed_cost_premium = 0`.

### 4e. Price-deal score

```
price_deal_score = (expected_value - asking_price - landed_cost_premium) / expected_value
```

Positive = underpriced after landed costs. The threshold for notification is 0.15 (≥15% below adjusted fair value, after transport).

`suspicious_underprice` flag fires (does not block notification, just labels) when `asking_price < value_low × 0.85`.

### 4f. Flag score

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

### 4g. Notification rule

For a hot-deal push notification:
- No showstopper flag.
- `confidence_bucket ∈ {medium, high}` (≥5 comps).
- `price_deal_score ≥ 0.15` **AND** `flag_score ≥ -1`.
- Vehicle is in the active search filter.
- Not already notified (dedup by listing ID; re-notifies on price drops if score improves by ≥0.05; re-notifies on `Maybe` user_action; suppressed on `Passed`).
- Not in quiet hours (22:00–08:00 local), unless `price_deal_score ≥ 0.30`.

For dashboard "watchlist":
- `price_deal_score ≥ 0.08` **OR** `flag_score ≥ +2`.

### 4h. Score versioning

Every scored row records `scoring_version` and `weights_hash`. Weight changes don't retroactively rewrite history; backfill is an explicit admin action. Lets us A/B-test future weight changes against `historical_sales` outcomes.

### 4i. Calibration (phase 2)

When `historical_sales` accumulates enough data:
- Replace P10/P90 spread with median-price-per-condition-bucket curve, learned per make/model.
- Calibrate the asking-to-sold haircut from real listing-to-disappearance pairs.
- Calibrate flag weights from cross-correlation with `days_listed` and `disposition_reason`.

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
- Canonical taxonomy of red/green flags (from §4f).
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

---

End of design.
