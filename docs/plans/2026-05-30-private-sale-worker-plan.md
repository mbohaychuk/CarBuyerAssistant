# Private-Sale Worker Implementation Plan (PR-2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The working private-sale pipeline — a `private_sale` cron worker that
scrapes Kijiji every ~15 min, upserts `private_listings`, then enriches, values,
matches, and (when a deal or a saved-search match) posts a real-time Discord
alert — reusing `llm/` and `scoring/` as libraries.

**Architecture:** One self-contained cron worker (the `auction_digest` pattern:
`main(now=None)`, singleton lock, Discord POST outside the DB tx). Per listing,
in order: enrich (call `provider.describe()`, apply output to the row), value
(call `scoring/`), match (`adapt_private_listing` → `match_listing` → insert
`SavedSearchMatch`), alert. The auction enricher/valuator workers are NOT
touched — the worker calls the same pure libraries.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (async), aiohttp (Discord),
httpx + selectolax (scraping), OpenAI (enrichment via `llm/`), pytest, ruff,
pyright (strict).

**Spec:** `docs/specs/2026-05-30-private-sale-intake-design.md`. **Depends on
PR-1 (merged):** the `PrivateListing` model + `adapt_private_listing` adapter.

---

## Decisions / hard constraints

1. **The Kijiji parser needs real HTML fixtures the human captures.** Neither
   the implementer nor CI can fetch live Kijiji (anti-bot + no network). So the
   ENTIRE worker pipeline (Tasks 1–4) is built and tested against a **fake
   source** producing `RawPrivateListing` objects + a **mocked LLM provider** +
   a **mocked Discord post** — fully TDD-able with zero network. The real Kijiji
   scraper (Task 5) is isolated and gated on captured fixtures
   (`tests/sources/kijiji/fixtures/*.html`). If fixtures aren't available, Tasks
   1–4 still ship a complete, tested pipeline; Task 5 lands when fixtures exist.
2. **Reuse confirmed (PR-2 planning task 0, done):** `provider.describe()` is
   pure (`DescribeInput`→`EnrichmentOutput`); the whole `scoring/` library is
   pure and `AuctionLot`-free. So no refactor of the auction enricher/valuator —
   the worker calls these libraries directly. The only new "apply" logic is
   mapping `EnrichmentOutput` → `PrivateListing` columns (Task 2), mirroring the
   auction enricher's `_apply_to_lot` field assignments.
3. **Private pricing:** `all_in_cost`/`price_deal_score` are called with
   `current_high_bid=ask_price_cad`, `buyer_premium_pct=Decimal(0)`,
   `gst_pct=Decimal(0)`, `pst_pct=Decimal(0)` (no buyer premium/tax on a private
   sale), and the real `landed_cost_premium(home=settings.home_province,
   dest=pickup_province, ...)`. Comps come from `historical_sales` exactly as
   the auction valuator does; the auction-vs-private comp-channel bias is a
   documented follow-on (spec Risks), MVP uses the existing `expected_value`.
4. **Alert:** fire when `price_deal_score >= settings.private_deal_threshold`
   OR the listing produced ≥1 saved-search match, AND `user_action != passed`.
   Plaintext via `post_simple_message` to the resolved `private_deals` channel
   (fallback `early_warning`). Dedup via `alerted_at`; re-alert only on a price
   drop ≥ `settings.private_realert_drop_pct` below `last_alert_price_cad`.
5. **Worker = cron, every 15 min, singleton-locked** (digest pattern). It
   processes listings whose `enrichment_status`/`valuation_status` is pending
   (this cycle's new/changed + prior-cycle failures), each in its own short tx;
   the alert POST is outside the tx (PR-3's duplicate-post fix).

## Context the implementer must know (confirmed entry points)

- **Enrichment (pure):** `from carbuyer.llm.base import DescribeInput`;
  `from carbuyer.llm.openai_provider import OpenAIProvider`;
  `await provider.describe(DescribeInput(...)) -> EnrichmentOutput`
  (`carbuyer.llm.schemas.EnrichmentOutput`). `DescribeInput` fields: `lot_id,
  title, description, year, make, model, auctioneer_name, auction_subtype,
  pickup_province, raw_carfax_url, current_high_bid_cad, bid_increment,
  auction_close_at, is_no_reserve, image_count, current_year`. `find_carfax_url`
  is in the enricher module — re-implement the one-liner or import it. The
  auction enricher's `_apply_to_lot` (in `apps/enricher/enricher.py`) shows the
  EnrichmentOutput→row mapping to mirror.
- **Valuation (pure):** `from carbuyer.scoring.comps import build_comp_set`
  (async, takes session + make/model/trim/year/mileage_km);
  `from carbuyer.scoring.fair_value import compute_fair_value` (comps +
  condition + sparse → `FairValue` with value_low/mid/high/expected_value_cad/
  comp_count/confidence); `from carbuyer.scoring.score import RarityInputs,
  rarity_score, flag_score, all_in_cost, price_deal_score`;
  `from carbuyer.scoring.landed_cost import distance_km_between,
  landed_cost_premium`. The auction valuator's `value_one` (in
  `apps/valuator/valuator.py`) is the call-sequence template.
- **Source scaffold:** mirror `src/carbuyer/sources/mcdougall/{source.py,parser.py}`
  (selectolax `HTMLParser`, `tree.css`/`css_first`, paginated walk, `jittered_sleep`).
  Reuse `sources/http.py` (`make_client`, `jittered_sleep`) + `sources/retry.py`
  (`RetryTransport`). `register()` + `SOURCES` from `sources/base.py`.
- **Worker shape:** mirror `src/carbuyer/apps/auction_digest/` (PR-3) — cron
  `main(now=None)`, `run_worker("private_sale", main)`, `acquire_singleton_lock`,
  `run_cycle(now)` test seam, per-listing try/except, post-outside-tx-then-stamp.
- **Discord:** `post_simple_message` from `notifier/discord_post.py`;
  `resolve_channels` from `notifier/channel_resolver.py`. New `private_deals`
  channel key (fallback `early_warning`).
- **Tests:** `carbuyer_test` DB; pure scoring tested without DB; the worker
  tested with a fake source + monkeypatched `provider.describe` + monkeypatched
  `post_simple_message`. Fixture-based parser tests for Kijiji (Task 5).

## File structure

```
Create:
  src/carbuyer/sources/base.py            (+ RawPrivateListing dataclass)   [Modify]
  src/carbuyer/db/private_upsert.py        (upsert_private_listing + status cascade)
  src/carbuyer/apps/private_sale/__init__.py        (empty)
  src/carbuyer/apps/private_sale/__main__.py        (run_worker entry)
  src/carbuyer/apps/private_sale/enrich.py          (DescribeInput build + apply EnrichmentOutput -> PrivateListing)
  src/carbuyer/apps/private_sale/value.py           (scoring/ reuse -> PrivateListing)
  src/carbuyer/apps/private_sale/worker.py          (cron main + run_cycle: scrape->upsert->enrich->value->match->alert)
  src/carbuyer/sources/kijiji/__init__.py           (empty)
  src/carbuyer/sources/kijiji/source.py             (KijijiSource)
  src/carbuyer/sources/kijiji/parser.py             (parse_search_page, parse_listing_detail)
  infra/systemd/carbuyer-private-sale.{service,timer}
  tests/apps/private_sale/{__init__.py,test_enrich.py,test_value.py,test_worker.py}
  tests/sources/kijiji/{__init__.py,test_parser.py,fixtures/*.html}

Modify:
  src/carbuyer/shared/config.py            (+ private_deal_threshold, private_realert_drop_pct, private_provinces)
  src/carbuyer/db/saved_searches.py        (already has adapt_private_listing from PR-1)
```

---

## Task 1: `RawPrivateListing` + `upsert_private_listing`

**Files:** Modify `sources/base.py`; Create `db/private_upsert.py`; Modify
`shared/config.py`; Test `tests/apps/private_sale/test_upsert.py`.

- [ ] **Step 1: Add config knobs.** In `shared/config.py`:
```python
    private_deal_threshold: float = 0.15           # min price_deal_score to alert
    private_realert_drop_pct: float = 0.10          # re-alert if price drops >= this fraction
    private_provinces: tuple[str, ...] = ("AB", "SK", "MB")  # Kijiji search scope
```

- [ ] **Step 2: Add `RawPrivateListing`** to `sources/base.py` (mirrors `RawLot`, private-shaped):
```python
@dataclass(slots=True)
class RawPrivateListing:
    """A private-party listing parsed from a classifieds source, ready to upsert
    into private_listings. No auction fields (no bids, premium, close time).
    year/make/model are best-effort from the listing; the enricher normalizes."""
    source: str
    source_listing_id: str
    url: str
    title: str | None
    description: str | None
    photos: list[str] = field(default_factory=list)
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    mileage_km: int | None = None
    vin: str | None = None
    ask_price_cad: Decimal | None = None
    pickup_province: str | None = None
    pickup_city: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 3: Write failing upsert test** (`tests/apps/private_sale/test_upsert.py`): a fresh `RawPrivateListing` → inserts a `PrivateListing` with `enrichment_status='pending'`; re-upsert with a changed `ask_price_cad`/`title` → updates + resets statuses to pending + bumps `last_seen_at`; re-upsert unchanged → no status reset. Use the `session` fixture; canonicalize the url via `sources.resolver.canonicalize_url`.

- [ ] **Step 4: Implement `db/private_upsert.py`** — `async def upsert_private_listing(session, raw: RawPrivateListing) -> PrivateListing` using `pg_insert(PrivateListing).values(...).on_conflict_do_update(index_elements=["source","source_listing_id"], set_={...})` with `coalesce(EXCLUDED, table)` for non-null fields, always bumping `last_seen_at=func.now()`. A content-change check (title/description/photos/ask_price_cad differ, OR a new insert) resets `enrichment_status`/`valuation_status` to `'pending'` (mirror `upsert_lot_with_status_cascade`). Set `canonical_url` via `canonicalize_url(raw.url)`.

- [ ] **Step 5: Run tests → PASS. Commit:** `feat(private_sale): RawPrivateListing + upsert with status cascade`.

---

## Task 2: Enrichment — build `DescribeInput`, apply `EnrichmentOutput`

**Files:** Create `apps/private_sale/enrich.py`; Test `tests/apps/private_sale/test_enrich.py`.

- [ ] **Step 1: Write failing test** with a FAKE provider: a `PrivateListing` (raw title/description, year/make/model None) + a stub `describe()` returning a canned `EnrichmentOutput` → `enrich_private_listing` applies normalized year/make/model/trim, `title_status`, `condition_categorical`, `red_flags`/`green_flags`/`showstopper_flags`, `summary`, `desirable_trim_or_spec`, `classic_or_collector`, `description_quality`, and sets `enrichment_status='done'`. Verify `DescribeInput` is built with `auction_subtype="private"`, `current_high_bid_cad=ask_price_cad`, `bid_increment=None`, `auction_close_at=None`, `auctioneer_name=None`, `image_count=len(photos)`.

- [ ] **Step 2: Implement `enrich.py`:**
```python
async def enrich_private_listing(listing: PrivateListing, *, provider: DescribeProvider) -> None:
    payload = DescribeInput(
        lot_id=listing.id, title=listing.title or "", description=listing.description or "",
        year=listing.year, make=listing.make, model=listing.model,
        auctioneer_name=None, auction_subtype="private",
        pickup_province=listing.pickup_province,
        raw_carfax_url=find_carfax_url(listing.description or ""),
        current_high_bid_cad=listing.ask_price_cad, bid_increment=None,
        auction_close_at=None, is_no_reserve=False,
        image_count=len(listing.photos or []), current_year=datetime.now(UTC).year,
    )
    out = await provider.describe(payload)
    _apply_enrichment(listing, out)   # maps EnrichmentOutput -> PrivateListing columns
    listing.enrichment_status = EnrichmentStatus.DONE.value
```
`_apply_enrichment` mirrors the auction enricher's `_apply_to_lot` field mapping (read it; map the SAME EnrichmentOutput fields to the identically-named `PrivateListing` columns). Re-implement `find_carfax_url` (a small regex over the description) or import it from the enricher module.

- [ ] **Step 3: Run → PASS. Commit:** `feat(private_sale): enrich a private listing via the LLM library`.

---

## Task 3: Valuation — reuse `scoring/`

**Files:** Create `apps/private_sale/value.py`; Test `tests/apps/private_sale/test_value.py`.

- [ ] **Step 1: Write failing test** (DB: seed `historical_sales` comps like `tests/apps/test_valuator.py` does): an enriched `PrivateListing` (make/model/year/mileage/condition set) → `value_private_listing` sets `expected_value_cad`, `value_low/mid/high`, `comp_count`, `confidence_bucket`, `rarity_score`, `flag_score`, `all_in_cost_cad` (= ask + landed, no premium/tax), `price_deal_score`, and `valuation_status='done'`. Assert a clearly-underpriced ask yields a positive `price_deal_score`.

- [ ] **Step 2: Implement `value.py`** following the auction valuator's `value_one` sequence but for a `PrivateListing` with no premium/tax:
```python
async def value_private_listing(session: AsyncSession, listing: PrivateListing) -> None:
    if listing.make is None or listing.model is None or listing.year is None:
        listing.valuation_status = ValuationStatus.INSUFFICIENT.value
        return
    comps = await build_comp_set(session, make=listing.make, model=listing.model,
                                 trim=listing.trim, year=listing.year, mileage_km=listing.mileage_km or 0)
    fv = compute_fair_value(comps, condition=listing.condition_categorical or "decent", sparse=False)
    listing.value_low_cad, listing.value_mid_cad, listing.value_high_cad = fv.value_low_cad, fv.value_mid_cad, fv.value_high_cad
    listing.expected_value_cad, listing.comp_count, listing.confidence_bucket = fv.expected_value_cad, fv.comp_count, fv.confidence.value
    hist = await _historical_comp_count(session, make=listing.make, model=listing.model)
    listing.rarity_score = rarity_score(RarityInputs(listing.desirable_trim_or_spec, listing.classic_or_collector, hist, None))
    listing.flag_score = flag_score(listing.red_flags or [], listing.green_flags or [], description_quality=None)
    dest = listing.pickup_province or settings.home_province
    landed = landed_cost_premium(home=settings.home_province, dest=dest, distance_km=distance_km_between(settings.home_province, dest))
    if listing.ask_price_cad is not None:
        listing.all_in_cost_cad = all_in_cost(current_high_bid=listing.ask_price_cad, buyer_premium_pct=Decimal(0),
                                              gst_pct=Decimal(0), pst_pct=Decimal(0), landed_cost_premium=landed)
        if fv.expected_value_cad is not None:
            listing.price_deal_score = price_deal_score(current_high_bid=listing.ask_price_cad, buyer_premium_pct=Decimal(0),
                                                        gst_pct=Decimal(0), pst_pct=Decimal(0), landed_cost_premium=landed,
                                                        expected_value=fv.expected_value_cad)
    listing.valuation_status = ValuationStatus.DONE.value
```
(`_historical_comp_count` mirrors the valuator's helper — a `func.count()` over `HistoricalSale` by make/model. Confirm `compute_fair_value`'s `ConfidenceBucket` `.value` and whether INSUFFICIENT confidence should leave `price_deal_score` None.)

- [ ] **Step 3: Run → PASS. Commit:** `feat(private_sale): value a private listing via the scoring library`.

---

## Task 4: The worker — orchestrate + match + alert (fake source)

**Files:** Create `apps/private_sale/{__init__.py,__main__.py,worker.py}`,
`infra/systemd/carbuyer-private-sale.{service,timer}`; Test
`tests/apps/private_sale/test_worker.py`.

- [ ] **Step 1: Write failing worker tests** with a FAKE source (an object whose
  `iter_search_results`/`fetch_listing_detail` yield canned `RawPrivateListing`s),
  a monkeypatched `provider.describe` (canned `EnrichmentOutput`), seeded comps,
  and a monkeypatched `post_simple_message` (capture posts). Cover via
  `run_cycle(now, source=fake)`:
  - a listing that's a deal (price_deal_score ≥ threshold) → posts + stamps `alerted_at`;
  - a listing that matches a `SavedSearch` (but not a deal) → posts + a `SavedSearchMatch(source_kind="private_listing")` row exists;
  - a listing that's neither → no post, no alert;
  - a `user_action='passed'` listing → no post;
  - re-running the cycle on an already-alerted listing → no duplicate post;
  - a price drop ≥ `private_realert_drop_pct` → re-alerts;
  - post failure path (fake post returns False) → `alerted_at` stays NULL;
  - per-listing exception isolation (one raises → others still process).

- [ ] **Step 2: Implement `worker.py`:** `run_cycle(now, *, source, provider, http)`:
  1. scrape: `async for r in source.iter_search_results(...)` → `fetch_listing_detail` → collect `RawPrivateListing`s (bounded by `private_provinces`).
  2. upsert each via `upsert_private_listing` (own short tx).
  3. select pending listings (`enrichment_status='pending' OR valuation_status='pending'`).
  4. for each pending, in its own short tx: `enrich_private_listing` (if enrichment pending) → `value_private_listing` → match (`adapt_private_listing` + `match_listing` over active `SavedSearch`es → idempotent `pg_insert(SavedSearchMatch).on_conflict_do_nothing`).
  5. decide alert: `should_alert = (price_deal_score is not None and >= settings.private_deal_threshold) or matched`, and `user_action != passed`, and (`alerted_at is None` OR price dropped ≥ pct). Compose a plaintext message (vehicle line, ask vs expected + deal score, condition, matching search names, link).
  6. POST outside the tx via `post_simple_message`; on success stamp `alerted_at`/`last_alert_price_cad` in a short second tx.
  Per-listing `try/except` isolation; summary counters; `_resolve_private_channel()` (auction_digest pattern, `private_deals`→`early_warning` fallback). `main(now=None)`: singleton lock + aiohttp session + a REAL `KijijiSource` (Task 5) + `OpenAIProvider`, calling `run_cycle`. `__main__.py`: `run_worker("private_sale", main)`.

- [ ] **Step 3: systemd** — `carbuyer-private-sale.{service,timer}` mirroring the digest's (timer `OnUnitActiveSec=15min`, `Type=oneshot` service `-m carbuyer.apps.private_sale`).

- [ ] **Step 4: Run → PASS. Commit:** `feat(private_sale): cron worker — scrape/enrich/value/match/alert + systemd`.

---

## Task 5: Kijiji source + parser (GATED ON CAPTURED FIXTURES)

**Files:** Create `sources/kijiji/{__init__.py,source.py,parser.py}`,
`tests/sources/kijiji/{__init__.py,test_parser.py,fixtures/*.html}`.

> **PREREQUISITE (human):** capture real Kijiji HTML into
> `tests/sources/kijiji/fixtures/`: at least one vehicle search-results page
> (`search_page_1.html`) and 2 listing detail pages (`listing_detail_*.html`,
> ideally different layouts). The parser is written to match THESE fixtures.
> Without them, this task is blocked — Tasks 1–4 already deliver a complete,
> tested pipeline that runs against the fake source.

- [ ] **Step 1: Inspect the captured fixtures** — identify the CSS selectors for
  the search card (listing id, url, title, price, thumbnail) and the detail page
  (title, description, price, location→province/city, photo URLs).
- [ ] **Step 2: Write failing fixture-based parser tests** (mirror
  `tests/sources/mcdougall/test_source.py`): `parse_search_page(fixture)` →
  expected count + first card's exact fields; `parse_listing_detail(fixture, listing_id=...)`
  → exact title/price/location/photos. Empty/garbage page → `[]`.
- [ ] **Step 3: Implement `parser.py`** (`SearchResult`/`ListingDetail` dataclasses
  + `parse_search_page`/`parse_listing_detail` with selectolax, robust to missing
  fields) and **Step 4: `source.py`** (`KijijiSource(Source)`, `name="kijiji"`,
  `version="1"`, `__aenter__`/`__aexit__` with `make_client`+`RetryTransport`,
  `iter_search_results` paginating the `private_provinces` searches with
  `jittered_sleep`, `fetch_listing_detail` → `RawPrivateListing`; `register(KijijiSource())`).
- [ ] **Step 5: Run parser tests → PASS. Commit:** `feat(sources): Kijiji private-listing source + parser`.

---

## Task 6: Lint, type-check, full-suite gate + final review

- [ ] **Step 1: Ruff** on all new files → clean (noqa per codebase convention for test count/threshold literals; hoist inline imports unless circular).
- [ ] **Step 2: Pyright** strict on `apps/private_sale`, `sources/kijiji`, `db/private_upsert.py` → 0 new errors (the worker helpers typed `AsyncSession`, no `# type: ignore`).
- [ ] **Step 3: Full suite** `uv run pytest -q` → green (note the pre-existing wall-clock-flaky `test_today` may intermittently fail, unrelated).
- [ ] **Step 4: `python -c "import carbuyer.apps.private_sale.worker; import carbuyer.apps.private_sale.__main__"`** → clean.
- [ ] **Step 5: Final whole-PR review** (cross-component): the enrich→value→match→alert flow is coherent; the alert dedup + post-outside-tx is correct; the fake-source tests genuinely exercise the pipeline; the `private_deals` channel resolution + fallback works; no auction-only worker accidentally touches `private_listings`.
- [ ] **Step 6: Commit any fixes.**

---

## Self-review (controller checklist)

- **Spec coverage:** spec §Components 2 (Kijiji source) → Task 5; §Component 3
  worker (enrich/value/match/alert inline) → Tasks 2–4; §Component 4 alert +
  `private_deals` channel → Task 4; the reuse premise → confirmed (Decision 2);
  dedup/price-drop re-alert → Task 4; lifecycle (`removed_at` sweep) — note:
  the removed-listing sweep is folded into Task 1's upsert design or deferred to
  PR-3 (flag if not built). Dashboard (§Component 5) is PR-3.
- **Type consistency:** `RawPrivateListing` fields ↔ `upsert_private_listing` ↔
  `PrivateListing` columns; `DescribeInput`/`EnrichmentOutput` ↔ `_apply_enrichment`
  ↔ `PrivateListing` columns; `scoring/` signatures ↔ `value_private_listing`
  args; `adapt_private_listing` (PR-1) ↔ the match step.
- **Fixture dependency (Decision 1):** Tasks 1–4 are network-free and fully
  TDD-able; Task 5 is the only fixture-gated task and is last, so PR-2 delivers a
  tested pipeline even if Kijiji fixtures slip.
- **Placeholder scan:** the `_apply_enrichment` field mapping and the exact
  selectors (Task 5) are the two spots that read existing code / fixtures rather
  than quoting verbatim — both reference the concrete source to copy
  (`enricher._apply_to_lot`; the captured fixtures). No TBDs.
```
