# Phase 1 — `vehicle_offer` Schema Split: Migration Plan & Slice Map

**Date:** 2026-06-28
**Status:** Awaiting sign-off on (1) scope and (2) the irreversible migration choices in §2.
**Implements:** the locked §6-A storage decision in `2026-06-27-want-list-pivot-design.md`.
**Grounded in:** a 7-subsystem blast-radius map + an architect synthesis (workflow `wf_852ea734`), cross-checked against `db/{models,queue,notify,upserts}.py`.

---

## 0. The one-paragraph version

Today's monolithic `auction_lots` table splits into a `vehicle_offer` **parent** (everything source-agnostic — vehicle facts, enrichment, vision, valuation, the 4 pipeline-status columns + their queue indexes) and two **children** sharing its PK: `auction_lot` (bid state, lot_status, scheduled_end) and `private_listing` (asking_price, listing_status — ships **empty**). This is the most irreversible change in the project. We land it as **S1: a zero-behaviour-change refactor** — every existing test stays green, nothing user-visible moves — *before* any private-listing feature rides on it. The private source (Kijiji) and its scoring come in later slices.

---

## 1. Migration strategy: RENAME-IN-PLACE (recommended)

`ALTER TABLE auction_lots RENAME TO vehicle_offer`, then carve a new child `auction_lot` out of it.

**Why, vs fresh-tables-and-copy:** rename keeps every `id`, the `auction_lots_id_seq` ownership, all three inbound FK *values*, the four partial pending indexes, and the `upper()` functional index **physically untouched** — the migration is column-moves + constraint-retargets, not a row rewrite. Fresh-copy re-inserts every row (id/sequence drift, must re-point 3 FKs to copies, more autogenerate footguns) for zero benefit.

**Steps** (one hand-written Alembic revision — autogenerate cannot see a rename and will re-propose dropping the functional index):
1. `op.rename_table("auction_lots", "vehicle_offer")`. Sequence ownership + the 4 pending indexes + the functional index ride along on parent columns → **NOTIFY/queue keeps working on the same physical rows throughout.** Rename the indexes to `ix_vehicle_offer_*` to stay in lockstep with the model.
2. Create child `auction_lot`: `id BIGINT PK REFERENCES vehicle_offer(id)` (no own sequence) + the child columns (§3).
3. Move data: `INSERT INTO auction_lot (id, …) SELECT id, … FROM vehicle_offer`; backfill `vehicle_offer.offer_kind = 'auction'`; `DROP COLUMN` each child column from the parent; recreate child indexes (`lot_status`, per-lot `scheduled_end_at`).
4. Decompose the cross-table composite `ix_auction_lots_price_deal_score (price_deal_score, lot_status)` — can't span two tables: `price_deal_score` → parent index, `lot_status` → child index.
5. Retarget the 3 inbound FKs to `vehicle_offer(id)` (values unchanged — shared PK): `auction_bid_history.lot_id`, `purchases.linked_lot_id`, `want_matches.lot_id`.
6. Create `private_listing` **empty**: `id BIGINT PK REFERENCES vehicle_offer(id)` + `asking_price_cad, seller_type, days_on_market, listing_status, first_seen_at, disappeared_at` (+ natural key deferred to S2).

**Acceptance gate (built first):** an **automated migration round-trip test** — seed pre-split `auction_lots` rows → `alembic upgrade head` → assert `count(parent) == count(child) == count(pre)`, every old id has matching parent+child rows, all 3 FK columns still resolve → `alembic downgrade` round-trips. If automating Alembic-in-test proves infeasible against the test DB, fall back to a documented script run on a seeded local DB. Tests today only run `create_all`, so the migration itself is otherwise unverified — this gate is non-negotiable.

---

## 2. Irreversible choices that need your sign-off

| # | Decision | Recommendation | Why / alternative |
|---|----------|----------------|-------------------|
| A | **Migration approach** | Rename-in-place (§1) | vs fresh-copy: rename preserves ids/seq/FKs/indexes for free. |
| B | **Discriminator** | Add an `offer_kind` column on the parent + SQLAlchemy `polymorphic_on` | The spec's "absence of a child = discriminator" is the *intent*; SQLAlchemy joined-table inheritance can't infer subtype from which child has a row — it needs a stored column. Alternative (no column, query each child explicitly) loses the polymorphic loading the notifier/dashboard lean on. |
| C | **Keep `AuctionLot` Python class name** (now a JTI subclass of `VehicleOffer`) | Yes | ~4× test-cost swing: `AuctionLot(make=…, current_high_bid_cad=…)` still constructs+inserts both tables, so genuine test breakage is ~3–5 files, not the 20 that touch it. |
| D | **`private_listing` in S1** | Ship empty in S1 | Same irreversible migration touch; proves the 3-way mapper under `create_all` for ~10 lines. Writes wait for S2. |
| E | **Sequence** | Keep `auction_lots_id_seq` on `vehicle_offer.id`; children `autoincrement=False` | Survives `RENAME TABLE`; mint no new sequence (would break id preservation). |

---

## 3. Column placement (parent vs child)

**`vehicle_offer` (parent) — source-agnostic, the shared pipeline reads/writes here:**
- `id`, **`offer_kind`** (new), `created_at`, `updated_at`
- Vehicle facts: year, make, model, trim, engine, transmission, drivetrain, mileage_km, vin, title_status, province_of_origin
- Content (cascade-relevant): **url, title, description, photos, parser_version** — must be on the parent so the content-change cascade stays a single-row read
- Enrichment: condition_*, red/green/showstopper_flags, summary, carfax_*, description_quality, condition_inferred_from_sparse_listing
- Rarity/desirability: desirable_trim_or_spec, classic_or_collector, desirability_signals/evidence, historical_comp_count, recent_appreciation, rarity_score
- Vision: vision_*
- Valuation: comp_count, value_low/mid/high_cad, expected_value_cad, landed_cost_premium_cad, all_in_at_current_bid_cad, recommended_max_bid_cad (NULL for listings), price_deal_score, flag_score, confidence_bucket, suspicious_underprice_flag, scoring_version, weights_hash
- **Queue:** enrichment/valuation/vision/notification_status + their *_attempts/*_version/last_*_error, **last_notified_channel**
- User: user_action, notes, was_purchased_by_us

**`auction_lot` (child) — auction-specific:**
- `id` (PK = FK → vehicle_offer.id)
- auction_id (FK auctions + the `uq(auction_id, source_lot_id)` key), source_lot_id, source_lot_row_id, lot_number
- scheduled_end_at
- Bid state: current_high_bid_cad, last_bid_observed_at, bid_count_visible, reserve_met, lot_status, closed_at, final_bid_cad
- **All five trigger timestamps → child** (early_warning / cheap / closing / trajectory / extended `_notified_at`)
- relationships: `auction`, `bid_history`

> **Refinement vs the architect (decision F):** the architect split the five `*_notified_at` stamps (early_warning+cheap → parent, the rest → child). I put **all five on the child.** They're all legacy *auction*-trigger fire-once state; private listings alert via the `want_matches` ledger, not these columns — so "rarity alert for a listing" is YAGNI, and a clean rule (queue/delivery bookkeeping → parent; which-auction-trigger-fired → child) beats a per-trigger split. Veto if you'd rather keep the door open for offer-level legacy alerts on listings.

**`private_listing` (child) — ships empty in S1:** asking_price_cad, seller_type, days_on_market, listing_status, first_seen_at, disappeared_at.

---

## 4. The one genuinely tricky code change in S1: the upsert

`_upsert_lot` (`upserts.py:141`) is a single `INSERT … ON CONFLICT (auction_id, source_lot_id)` — but those keys now live on the **child**, while the content cols + cascade live on the **parent**. The ingester is **single-instance** (advisory-lock), so the existing pre-SELECT (`upserts.py:236`) makes this race-free: resolve the id, then UPDATE parent + UPDATE child if it exists, else INSERT parent (RETURNING id) → INSERT child. The content-cascade (resets the 4 statuses) moves to the parent write and is otherwise unchanged. Exact mechanism settled via TDD in S1.

Other forced S1 edits (all verify-by-running): `queue._mark_in_progress` / `recover_orphans` and `actions.rescore_all` do bulk `update(AuctionLot).values(<parent col>)` → repoint to `VehicleOffer` (Core UPDATE through the child mapper would target the child table and break). ORM attribute writes (e.g. bid_poller `lot.valuation_status = …`) are fine — unit-of-work splits the flush. `tools/value_pending_lots.py:258` has a raw `FROM auction_lots l`.

---

## 5. Slice map (smallest-blast-first, each independently shippable + green)

- **S1 — Schema split + JTI, zero behaviour change** ✅ **DONE** *(the irreversible one)*. §1–§4 landed: `vehicle_offer` parent + `auction_lot`/empty `private_listing` children via joined-table inheritance (`offer_kind` discriminator, `with_polymorphic="*"` for async-safe loading); rename-in-place migration `1d6201a6e2d0`; ORM two-table upsert; queue/`actions`/`needs_plugin`/tool repoints. **Verified three ways:** full suite **639 passed**; round-trip gate `scripts/verify_offer_split_migration.py` **PASS** (ids/FKs/values preserved up *and* down); migration↔model schema diff shows **zero drift** (only the pre-existing migration-only functional indexes + alembic's version table differ).
- **S2 — `ListingSource` sibling + `RawListing` DTO + `upsert_private_listing`** ✅ **DONE**. `ListingStatus` enum; `ListingRef`/`RawListing` DTOs; `private_listing` gains its `(source, source_listing_id)` natural key (migration `d85563d5b35f`, additive on the empty table); two-table ORM upsert sharing a new `_apply_content_cascade` helper with the lot path. Proven with hand-built `RawListing`s (no scraping). `ListingSource` is a kind-marker for now; `search_listings` lands in S5. 644 tests green; migration→model parity clean.
- **S3 — Channel-aware pipeline + §4c haircut.** ✅ **DONE**. The queue now claims the `vehicle_offer` parent (core-table `FOR UPDATE`, scoped past the `with_polymorphic` outer join) so private offers enter the pipeline; enricher/valuator/notifier load `VehicleOffer` and branch by subtype. New `scoring/asking_haircut.py` (§4c, calibratable); valuator prices listings off `asking_price_cad × (1−haircut)` with **no buyer premium and no GST**, province-based landed cost, `recommended_max_bid=None`. Want hand-off + matcher/deal/notifier widened to `VehicleOffer` (price/province injected — the Phase-0 source-agnostic seam paid off). Notifier branches: childless offer → want alerts only, no auction triggers, auction-free embed. `private_listing.location_province` added (migration `51338a1ffafb`). Vision/distiller stay auction-scoped (own queries). **651 tests** (7 new); migration↔model parity clean. Live private rows still don't flow until S5.
- **S4 — Kijiji source plugin** ⚙️ **SCAFFOLD DONE** (selectors pending real sample pages). Added `curl_cffi`; `CurlCffiTransport` (an `httpx.AsyncBaseTransport` doing TLS/JA3 impersonation, slotting under `RetryTransport`/`make_client`) — real + tested via an injected fake session. `settings.http_impersonate` + `proxy_url`. `ListingSource.search_listings(criteria)` is now the PULL contract; `KijijiSource` wires criteria→URL→fetch (tested via `httpx.MockTransport`) but `_parse_listings` raises `NotImplementedError` — correct selectors need real Kijiji HTML, and live-scraping a commercial site to harvest them is out of scope for an autonomous build. **Remaining for S4:** provide sample Kijiji pages → implement extraction + a fixture test; wire a production `PROXY_URL`. 657 tests green.
- **S5 — Want-list PULL ingestion (turn-on).** ✅ **DONE**. New ingester `listing_pull` strategy: load enabled wants → for each listing source × each want criteria, `search_listings` → `upsert_private_listing` + `NOTIFY enrichment_pending`, deduped on `(source, source_listing_id)` within a run. No-op without sources/wants; an unimplemented parser (Kijiji today) is isolated by the dispatch loop. Tested with a fake `ListingSource` (dedup, idempotent re-run, enabled-wants gating). Real listings will flow the S3-safe pipeline to a want alert once Kijiji selectors land. 661 tests.
- **S6 — Generalize `comps.build_comp_set` `sale_channel`** ✅ **DONE**. A third narrow SELECT over `private_listing` (make/model/year/mileage on the inherited parent; status/price on the child) contributes **disappeared** (sold/removed) listings as `private`-channel (1.00) comps — last-seen asking as a noisy sold proxy. Active listings are excluded (asking ≠ sale; would bias every fair value high). 662 tests.
- **S7 — vPIC make/model normalization** ✅ **DONE**. New `normalize/vpic.py` (`canonical_model` via NHTSA `GetModelsForMakeYear`, keyless, best-effort — any failure/no-match returns the input). Wired into the enricher's out-of-txn I/O block (after the LLM, before the write); `_apply_to_lot` takes a `canonical_model` override. Gated by `settings.vpic_normalization_enabled` (**off by default** — opt-in so it adds no NHTSA call to existing flows). Tested: vpic unit (F150→F-150, no-match/error→input) + enricher end-to-end (LLM 'F150' → row 'F-150'). 670 tests.

**Legal/PII hard rules carried from research:** never store/rehost listing photos (metadata + deep-link only); never persist seller PII.

---

## 6. Recommendation

Do **S1 only** this round — land the schema split isolated, green, with its migration-verification gate — then pause for review before building private-listing features on top. It's the load-bearing, hard-to-reverse foundation; isolating it de-risks S2–S7 and matches the Phase 0 slice-and-sign-off rhythm.
