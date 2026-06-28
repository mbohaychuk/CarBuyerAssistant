# Want-List Pivot — Design & Research

**Date:** 2026-06-27
**Status:** Proposal / awaiting direction decisions
**Supersedes the operating premise of:** `2026-05-08-carbuyer-mvp-design.md` (system-defined flip-finder)

---

## TL;DR

- **The pivot:** from a *system-defined auction flip-finder* ("surface any lot that's a good deal to make money") to a *user-defined want-list buying assistant* ("here are my target vehicles / archetypes — monitor auctions **and** private-sale sites, aggregate every match, highlight genuinely good deals relative to **my** want, and push-notify me").
- **The single most important finding: the spine is already half-built.** A dormant `searches` table, channel-normalized "private-sale-equivalent" valuation, a comps table already keyed on `sale_channel`/`sale_platform`, a reserved `SourceType="listing"`, a named-but-unbuilt asking-to-sold haircut seam, and an *embryonic* want-list (`derive_watched_make_model`) all already exist. This is an **activate-and-extend** job far more than a greenfield build.
- **The whole pivot UX can ship on existing auction data first** — wire the want-list → matcher → want-driven alerts with **zero new scrapers** — then add private-sale sources incrementally. That de-risks the product before the anti-bot arms race.
- **Decisions locked (2026-06-27, see §6):** (A) storage = **supertype/subtype** — a `vehicle_offer` parent with `auction_lot` + `private_listing` children, split **deferred to Phase 1**; (B) build the **want-list spine on auction data first** (zero scrapers); (C) first private source = **Kijiji, DIY scrape**.

---

## 1. What the app is today

A mature 10-process Python pipeline (Python 3.13 asyncio, SQLAlchemy 2 async, **Postgres-17-as-queue** via `NOTIFY`/`LISTEN` + `SKIP LOCKED`, FastAPI+HTMX dashboard, Discord bot, OpenAI `gpt-5-nano` text+vision). It ingests used-vehicle **lots** from Western-Canadian online **auctions** (HiBid GraphQL, McDougall), enriches+scores them with an LLM, polls bids on a soft-close-aware cadence, and pushes two kinds of Discord alert: "rare — drive out and look" and "closing cheap right now."

```
ingester → enricher(LLM) → valuator(comps+fair_value) → notifier(Discord)
              ▲                                            ▲
        bid_poller (tiered cadence)              vision_batcher (nightly)
                                                 auction_distiller → historical_sales
```

Sources are a plugin registry (`@register`, `sources.base`). Scoring is a pure-function layer (`scoring/`: comps → fair_value → landed_cost → score). Alerts are system-threshold-driven (`notifier/triggers.py`); the **only** user input today is a global per-lot `user_action` (interested/maybe/not-interested) written by three Discord buttons.

## 2. The pivot, precisely

| | Today (system-defined) | Pivot (user-defined) |
|---|---|---|
| **Driver** | "Is this a deal?" — system judgment (rarity + price_deal_score) | "Does this match **my** want, and is it a good deal **for that want**?" |
| **Surface** | Auctions only (HiBid, McDougall) | Auctions **+** private-sale sites (Kijiji, FB Marketplace, …) |
| **Want** | *Derived* from past clicks (`derive_watched_make_model`) | *Declared* saved-search / archetype with explicit criteria |
| **Economics** | Flip resale margin (`recommended_max_bid`, `flip_margin`) | Personal buyer — "below my ceiling / below fair value by X" |
| **Alerts** | Time-gated auction triggers (closing-soon, soft-close extend) | New-match + price-drop, per-want, want-relative |

It is a genuine **inversion of the scoring premise**, but it rides almost entirely on existing plumbing.

## 3. Key finding — the pivot is already scaffolded

Evidence that the original design *anticipated* this (so we extend, not fight):

- **`searches` table is the want-list home, and it's dead code.** `models.py:518` — `user_id` (default `"me"`), `name`, `config` JSONB (untyped, default `{}`), `enabled`. Zero readers/writers anywhere outside the model + initial migration. It is dormant scaffolding waiting to be wired.
- **Valuation already targets private-sale value.** `scoring/channels.py` `CHANNEL_MULTIPLIERS` normalizes every comp to private-party-equivalent (`private`=1.00, `dealer`=0.92, `auction_estate`=1.20); `expected_value` is *always* "what it'd clear at in a private sale." A private listing enters the comp machinery at multiplier **1.00 with no new math**.
- **Comps are already channel-discriminated.** `historical_sales` (`models.py:440`) carries `sale_channel`, `sale_platform`, `final_listed_price_cad`, `days_listed`, `observed_first_at`/`disappeared_at` — built to hold non-auction marketplace rows.
- **The source layer reserves the listing kind.** `sources/base.py:11` `SourceType = Literal["listing","auction"]`; `Source` base is already type-agnostic. The `ListingSource` class was specced (MVP repo-layout comment) and never built.
- **The asking-to-sold haircut seam is named-but-unbuilt.** MVP spec §4c reserves the discount for "when listings come back." This is the one genuinely missing scoring piece for fixed-price listings.
- **An embryonic want-list push already runs.** `today_queries.py:138` `derive_watched_make_model()` infers watched `(make,model)` from prior clicks; `alerts_since()` pushes "new lots matching watched make/model" into the Today inbox. The pivot replaces *derived* with *declared* criteria.
- **The browse-feed filters are the want-list template.** `feed.py` `_PRESETS` + province / free-text / `min_score` / `min_rarity` / `max_price_cad` / `watched_only` / `hide_showstoppers` — a want is literally one of these filter sets, named, persisted as a `Search` row, and run as a saved alert.
- **The desirability taxonomy already encodes the owner's archetypes.** `flags/taxonomy.py` `DESIRABLE_TRIMS` already lists **Lexus GX 470/GX 460** ("Body-on-frame Land Cruiser cousin"), 4Runner/Tacoma TRD, Wrangler Rubicon, with `is_desirable_trim`/`_norm` fuzzy matchers — a system-curated analog to a per-user want; a strong seed for archetype matching.

## 4. Reuse / Extend / Build-new

**Reuse as-is (free):**
- Postgres `NOTIFY`/`LISTEN` + `SKIP LOCKED` queue, `queue.py` claim/recover/partial-index pattern, advisory-lock single-instance discipline.
- Four pipeline-status columns (enrichment/valuation/vision/notification) — source-agnostic; the **content-change cascade** (`upserts.py:274` re-sets `notification_status=PENDING` on listing change) **is already the "changed listing" trigger** the pivot needs for price-drop re-alerts.
- `scoring/`: `fair_value` (pure function over a comp list), `landed_cost` (province→province), `flag_score`, and the `price_deal_score` *formula* `(EV − all_in)/EV` (works for any price input — pass `buyer_premium_pct=0`, `bid=asking_price`).
- `discord_post` REST + retry, `channel_resolver`, notifier worker skeleton + quiet-hours.
- Transport/retry/URL utils (`sources/http.py`, `retry.py`, `resolver.py`), the `@register`/`SOURCES` registry, the **HiBid GraphQL-reconstruction playbook**.
- LLM `NormalizedVehicle` (emits `transmission`/`drivetrain` — satisfies "manual-only" / "4wd" wants post-enrichment).

**Extend:**
- `searches.config` → a **typed Pydantic criteria schema** (make/model/year-range/trim/transmission/drivetrain/price-ceiling/region/condition + fuzzy-archetype free-text).
- `derive_watched_make_model` consumers → read **declared** `Search.config` instead of inferred `(make,model)`; generalize the `tuple_(make,model).in_()` predicate (reuse `feed.py`'s query builder).
- `notifier/triggers.py` → add want-relative triggers (`want_match`, `price_drop`) that don't depend on `scheduled_end`; `channels.py select_channel` → add a want/per-user routing dimension.
- `valuator` → generalize over a listing entity (asking_price vs current_high_bid; private tax/BP rate set vs auction BP/GST/PST); make `all_in_cost`'s tax inputs private-sale-aware (CA private used sales typically **no GST**, provincial tax at registration).
- `comps.build_comp_set` → generalize the hardcoded `sale_channel='auction_estate'` tag into per-source channels so private asking-price comps feed the same comp set.
- Bot buttons `deal:{action}:{lot_id}` → carry `want_id`; `taxonomy.DESIRABLE_TRIMS`/`_norm` → seed per-want matching.

**Build-new:**
- **Typed want-list criteria + a listing↔want matcher** (structured match now; LLM archetype expansion later) producing the set of wants a listing satisfies.
- **Per-`(listing, want, trigger)` dedupe ledger** (small join table) — the per-lot single `*_notified_at` columns *cannot* express per-want fire-once and will silently under-notify if reused.
- **A want-relative deal score** (good relative to *this* want's ceiling/comp set) distinct from the global `price_deal_score`.
- **`ListingSource` sibling** (discover/fetch, **no** bid-poll) + `RawListing` DTO (asking_price, transmission, seller_type, region, days-on-market, `listing_status` active/sold/removed/price_changed) + `upsert_listing`.
- **`asking_price_cad` column** + a `source_kind`/`listing_type` discriminator (today there is **no** fixed-price column anywhere; `RawLot.extra` carries `buy_now_price_cad` but it's dropped on upsert).
- **`/want` Discord slash-commands** — the bot has **zero** commands today (`tree.sync` is a no-op).
- **§4c asking-to-sold haircut** — so an *asking* price scores as a deal vs an expected *clearing* value.
- **Private-sale source plugins** + a TLS-impersonation fetch substrate for anti-bot sites.

## 5. Research findings (2026)

### 5a. Private-sale data sources (Western Canada), ranked
1. **Kijiji Autos — the durable backbone.** ~100k+ active listings, richest structured fields (VIN, Carfax link, trim, transmission, GPS), strongest AB/BC/SK/MB private coverage, **lightest anti-bot** (reCAPTCHA Enterprise, concentrated on *write* actions, not browse). No official API. **First target.**
2. **AutoTrader.ca — dealer comps, GraphQL-reconstructable.** Dealer-dominated (retail markup) → use as a **fair-value/comps** signal, not a private-deal firehose. Its `robots.txt` exposes internal GraphQL (`/listing-search-api/graphql`) → the **exact HiBid reconstruction technique transfers**. Caveat: owned by Trader Corp (litigant in the CarGurus precedent below).
3. **Facebook Marketplace — biggest private pool, hardest.** No Marketplace API; internal GraphQL behind a *dismissible* login modal (not hard auth), WAF + device fingerprinting + weekly CSS rotation → needs residential/mobile proxies + browser or a **pay-per-success API** (Apify/Zyte). Essential for volume, but highest legal+technical friction → **later, behind a paid API.**
4. **CarGurus.ca — price-vs-market intelligence, DataDome-hard.** Best deal-rating signal in NA; protected by DataDome → comps input via pay-per-success only.
- **Traps to skip:** Craigslist (aggressive litigation: $60.5M/$31M judgments + thin CA inventory), PrivateAuto (US-only), Cars.com (US-only), eBay (sanctioned EBAY_CA Browse API exists but thin local vehicle inventory — one cheap probe, low ROI), Clutch/Canada Drives (online *retailers* → comps at best).

### 5b. Legal hard-rules (Canada)
- **Never store or re-display listing photos.** *Trader Corp v. CarGurus* (Ontario 2017) found copyright infringement on 152,532 scraped listing photos; fair-dealing and "information-location-tool" defenses **rejected**. Store **derived metadata only** (price, specs, mileage, location, **source URL**) and **deep-link**. This single rule removes the only Canadian-precedent-backed exposure specific to this domain.
- **Minimize seller PII.** PIPEDA's "publicly available" exception is narrow; don't persist seller names/photos/phone — store the URL and let the human open it to contact.
- **Public-listing scraping is roughly CFAA-safe** (hiQ/Van Buren) but **ToS breach + copyright remain**; anything **behind a login** (e.g. FB contact details) flips back to "unauthorized access." Single-user, non-republishing, no-paywall-bypass, human-cadence throttling keeps the project in the low-risk band. (The team already learned this hands-on — the FarmAuctionGuide auto-discovery router was built then **abandoned** for human-in-the-loop after hitting Cloudflare; see `2026-05-16` Appendix A.)

### 5c. Valuation / comps stack
- **Keep self-built comps.** The existing engine (channel-normalized comps → p10/p50/p90 → confidence buckets) already mirrors what CarGurus IMV does internally; you don't need to beat it, just anchor it.
- **VIN decode:** **NHTSA vPIC** — free, no key, decodes Canadian VINs ~95%; `GetModelsForMakeYear` normalizes make/model strings so LLM output and scraped listings resolve to the same keys.
- **Asking ≠ sold:** model a **tunable per-segment haircut** (~5% private / ~3% dealer to start), then **calibrate against your own "disappeared-listing" data** (last-seen asking of vanished listings ≈ a noisy sold proxy). The system already records `observed_first_at`/`disappeared_at` — capture this delta.
- **Thin-comp fallback** (don't return INSUFFICIENT): progressively widen mileage ±30→50% / year ±1→2 → expand region (with adjustment) → trim-class → model-class → last-resort depreciation-curve roll from a populated sibling; tag a lower confidence bucket.
- **Cheap sanity anchor:** MarketCheck free tier (500 calls/mo) only when comps are thin. **Best paid reach** if self-built proves too thin: **VinAudit Canada** market-value API (~$100/mo, Canadian-native, returns a confidence band that maps onto p10/p50/p90).
- **Reliability evidence** for "easy-to-fix/reliable" archetypes: free **NHTSA complaints + recalls** APIs (structured known-issue counts) as the hard signal + LLM forum-sentiment summary as the soft signal. RepairPal/Consumer Reports have no usable free feed.

### 5d. Want-list & deal-alert UX patterns to copy
- **Two object types:** a **Want** (standing saved query + alert toggle) vs a **Watch** (a pinned specific listing tracked for price moves) — BaT's model.
- **Archetype → fan-out** (your differentiator nobody mainstream does): one fuzzy want ("cheap 4Runner-platform offroad base") expands to *multiple* concrete model searches (GX470 + 4Runner + LX470…) sharing one price/condition rule.
- **Deal score = price vs your own comp median, shown as both % AND $ below, plus comp count + a one-line "why."** Never a bare "Great Deal" badge — it's gameable (salvage/missing-options/stale-comp false positives). Show the evidence.
- **Tiered alerts:** instant ping for genuinely good matches (≤ target / great-deal) and watched-listing price-drops; **one daily digest** for everything else. Dedup by listing-id across sources; quiet hours; per-want mute.
- **Per-source signals:** auction cards = time-left / current bid / reserve; private cards = asking price / **days-on-market** / **price-drop history** (buyer leverage, not urgency). Sort a unified feed by deal-score; keep source-type + "ending soon" as filters/badges.

### 5e. Archetype matching + scraping infra
- **LLM-led hybrid, not a hand taxonomy.** `gpt-5-nano` (already in stack) expands a fuzzy archetype → structured `{make, model, year-range, trim, transmission, price-ceiling}` rows → normalize via vPIC → **owner confirms/edits**. The LLM *is* the taxonomy; the platform-relationship knowledge ("GX470 = J120 4Runner platform", "manual Xterra") is exactly the enthusiast-forum knowledge it has internalized. Seed/spot-check against `taxonomy.py`.
- **Scraping substrate:** reuse the HiBid GraphQL-reconstruction approach; default to **`curl_cffi`/got-scraping TLS-impersonation + one cheap Canadian residential proxy (~$1/GB)**. Escalate to a real browser only for hard-blocked sites — **`nodriver`/Patchright**, never the dead `playwright-extra`. For DataDome/Meta-class sites (CarGurus, FB), prefer a **pay-per-success API (Zyte)** over self-hosting the arms race. **Realistic personal-scale cost: $0–30/month.**

## 6. Decisions that gate the plan

### Decisions locked (2026-06-27)

**A — Storage: supertype/subtype (joined-table inheritance).** *Neither A1 (single nullable table) nor A2 (fully separate tables) — a shared parent.*
- `vehicle_offer` **(parent)** holds the PK + the source-agnostic columns: vehicle facts (year/make/model/trim/engine/transmission/drivetrain/mileage/vin/title/province), enrichment (condition, flags, summary, carfax), vision, valuation (value_low/mid/high, expected_value, price_deal_score), the **four pipeline-status columns + partial pending indexes + NOTIFY** (so one queue serves both channels), and `user_action`/`notes`.
- `auction_lot` **(child)** holds only auction-specific columns: bid state (current_high_bid, bid_count, reserve_met), `source_lot_row_id`, `lot_number`, per-lot `scheduled_end_at`, `lot_status`, `final_bid_cad`, the bid-dynamics notify timestamps; FK → its parent (shared PK).
- `private_listing` **(child)** holds: `asking_price_cad`, `seller_type`, `days_on_market`, `listing_status` (active/sold/removed/price_changed), `first_seen_at`/`disappeared_at`; FK → its parent (shared PK).
- **Why:** the shared pipeline reads/writes **one** table with no nulled-out junk and no UNION/discriminator branching; bid-poller only touches `auction_lot`, listing ingestion only touches `private_listing`; the *absence* of an `auction_lot` child is the discriminator. Cleaner than today's monolith for the shared path.
- **Cost:** the most invasive migration of the three — splits today's `auction_lots` monolith into parent+child, re-points every worker's column-ownership, moves FKs (`auction_bid_history`, `purchases.linked_lot_id`) + the queue indexes to the parent, adds SQLAlchemy joined-table polymorphism.
- **Timing:** **deferred to Phase 1** — Phase 0 ingests no private listings, so it builds on `auction_lots` as-is (matcher behind a thin query seam); the split lands when `private_listing` actually arrives. Validates the product before touching a load-bearing schema.
- *(Plugin layer, decided either way: a `ListingSource` **sibling** — no bid-poll — not an extension of the auction ABC, which McDougall already shows fabricates placeholder auctions.)*
- *Naming is reversible: `vehicle_offer`/`auction_lot`/`private_listing`.*

**B — Sequencing: want-list spine on existing auction data first.** Full pivot UX (declared wants → matcher → want-driven alerts → `/want` commands) on HiBid/McDougall data, **zero new scrapers**. Validates before the anti-bot fight.

**C — First private source: Kijiji, DIY** (`curl_cffi` TLS-impersonation + cheap CA residential proxy); AutoTrader.ca GraphQL as a dealer-comps feed alongside. FB Marketplace + CarGurus later, behind a pay-per-success API.

## 7. Phased plan

### Phase 0 — Want-list spine on auction data *(no schema split, no scrapers)* — ✅ COMPLETE (branch `want-list-pivot`)
All 9 slices shipped TDD-first, suite green. The loop works end-to-end on auction data: declare a want (`/want` Discord command or `/wants` dashboard) → valuator matches + scores it → notifier posts a want-relative alert. `carbuyer/wants/` holds criteria/repo/matcher/deal/service; `want_matches` is the fire-once ledger; the valuator and notifier are wired; management UI exists in Discord and the dashboard.

Slices (each compiles + tests green; TDD on the matcher/scoring logic):
1. **`WantCriteria` Pydantic schema** for `Search.config` — make[]/model[] (lists, so one want can name several models = manual fan-out), year_min/max, trim, transmission, drivetrain, price_ceiling_cad, provinces[], max_mileage_km, condition_min, hide_showstoppers. Round-trip ↔ the JSONB column.
2. **Want repository** — create/list/edit/enable/disable/delete `Search` rows.
3. **Matcher** — `matches(offer, criteria) -> bool` + a query builder returning matching `auction_lots`, **reusing `feed.py`'s province/ilike/price/transmission predicates** (generalize `tuple_(make,model).in_()` to multi-make/model). *Phase 0 = structured criteria only; LLM archetype expansion is Phase 2.* Behind a thin query seam so the Phase-1 parent/child split is contained.
4. **`want_matches` ledger** — `(search_id, offer_id, matched_at, want_relative_score, notified_at, dismissed)`, unique `(search_id, offer_id)` — replaces the per-lot single `*_notified_at` columns so each want fires once per listing independently.
5. **Want-relative deal score** — % and $ below `expected_value`/`value_mid`, vs the want's ceiling, + comp_count for the "why."
6. **Match-on-valuation hook** — when a lot finishes valuation (or content-change re-enqueue), evaluate vs enabled wants, upsert `want_matches`, enqueue notification. *Sub-decision: fold into the valuator's `_decide_notification_status` (recommended for P0) vs a dedicated `matcher` stage.*
7. **`want_match` notifier trigger + rendering** — fires off want-match + want-relative quality (no `scheduled_end` dependency); `render_want_match_text` ("Matches 'GX470 offroad base': $3,200 / 14% below market, 9 comps"). Reuse `discord_post`. *Sub-decision: route to a single `wants` channel / owner DM (P0) vs per-want channels (later).*
8. **`/want` Discord slash-commands** — add|list|remove|mute|edit. First app_commands tree (bot has none today); buttons carry `want_id`.
9. **Dashboard `/wants` UI** — create/edit a want from the existing feed-filter UI; matches-per-want; dismiss/pin. Reuse the `feed.py` query machinery + HTMX action pattern.

### Phase 1 — First private source (Kijiji) + AutoTrader comps
**Do the §6-A supertype/subtype split here** (`auction_lots` → `vehicle_offer` + `auction_lot`; add `private_listing`). Then: `ListingSource` sibling + `RawListing` DTO + `upsert_listing`; want-list **PULL** ingestion (query each active want per source); §4c asking-to-sold haircut (calibrated on disappeared-listing data); private-sale tax-aware `all_in`; `curl_cffi` + CA residential proxy; NHTSA vPIC make/model normalization. **Photos never stored — metadata + deep-link only.**

### Phase 2 — Archetype expansion + harder sources + price-drop alerts
LLM archetype expander (`gpt-5-nano` → vPIC-normalized → owner-confirm) seeded from `taxonomy.py`; NHTSA complaints/recalls reliability enrichment; FB Marketplace + CarGurus via pay-per-success API; price-drop / days-on-market tracking + re-alert; tiered instant-vs-daily-digest delivery; cross-source VIN dedup.

## 8. Open risks
- **Comp scarcity for fuzzy archetypes** — cross-platform wants (GX470-as-4Runner) have no single make/model to key comps on; needs model-set comp selection (§5c fallback).
- **Anti-bot is an arms race** — DataDome/Meta break unpredictably; treat no private source as a stable dependency; budget for breakage.
- **`'private'` multiplier 1.00 + uncalibrated §4c haircut** → listing deal scores optimistic until calibrated on real disappeared-listing data.
- **Single-user assumption is baked in** (`user_action` has no `user_id`; channels are global) — fine for "me", but any multi-user want-list needs attribution threaded through buttons/routing.
- **Flip economics** (`recommended_max_bid`, `flip_margin`, landed-cost) are flipper concepts — scope them to the auction half or drop for the buyer-assistant path.
- **`searches.config` has no versioning** — `WantCriteria` uses `extra="forbid"`, so a future rename/removal of a criterion would make `model_validate` reject pre-existing rows. Before any slice changes the criteria shape, add a `config` schema_version + migration (or isolate per-want validation so one stale row can't halt a batch match). Adding fields with defaults stays backward-safe.
