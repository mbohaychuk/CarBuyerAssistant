# Multi-source ingestion via farmauctionguide.com + McDougall

> **Status:** Shipped 2026-05-16. **FAG strategy dropped post-validation** — see Appendix A.
> **Scope:** (A) Wire `FarmAuctionGuideSource` into the ingester so we discover auctions across every platform FAG indexes, route known platforms to their plugins, and record unknown platforms for `/needs-plugin` triage. (B) Replace the McDougall stub with a real lot-first implementation (catalog walker + detail parser + bid poll). (C) Wire bid-poller dispatch for McDougall.
> **Recon (2026-05-16):** McDougall is server-rendered with a cross-auction Vehicles catalog at `products.php?category=Vehicles` (147 lots paginated). Lot detail (`products-full-view.php?arg=<GUID>`) exposes year/make/model/mileage/VIN/end-time/current-bid/photos without login. URL pattern is `?arg=<GUID>`, not `/auction/<id>` as the stub assumes.

## Problem

The ingester only knows about HiBid. Phase 10 of the auction-MVP plan landed `FarmAuctionGuideSource` (router, real selectors) and `McDougallSource` (registered, placeholder selectors) but never wired either into the worker. DB confirms: 25 auctions, all HiBid.

We want:
1. Auctions on AB/SK/MB/BC FAG province pages to appear in the dashboard, regardless of underlying platform.
2. HiBid auctions discovered via FAG to dedup against the existing GraphQL discovery (no double-write).
3. Unknown-platform auctions to surface in `/needs-plugin` with their host, no lots scraped (per user decision 2026-05-16).
4. A clean seam to plug in real McDougall fetchers later without re-architecting the ingester.

## Non-goals

- Generic shallow fetch for unknown platforms. User explicitly chose `/needs-plugin`-only.
- Per-source poll cadence customization. McDougall reuses the existing tiered cadence; differentiation per-source is a later refinement.
- ~~Buyer-premium cap/floor modeling~~ — **promoted into scope (2026-05-16)**. New nullable columns `buyer_premium_max_cad` + `buyer_premium_min_cad` on `auctions`. McDougall sets 2000/20; HiBid sets None/None.
- Modeling McDougall live (in-room) auctions. Out of scope — `products.php?category=Vehicles` is online-timed lots only, which is what we want anyway.
- Schema migrations beyond what's already nullable. `auctions.source_auction_id` is `str`, holds GUIDs fine.

## Constraints (load-bearing decisions, do not silently weaken)

- **Failure isolation:** one province/source failing must not abort the sweep. FAG already does per-province try/except; the ingester loop must do per-source try/except.
- **Dedup is upsert-driven:** `(source, source_auction_id)` is the natural key. Two discoverers finding the same HiBid auction collapse onto one row via `upsert_auction`. **Verify** this on first run — if HiBid's GraphQL flow and FAG's router emit different `source_auction_id`s for the same auction, we have a problem.
- **No lot scraping for `unknown:` sources.** Writing the auction row is intentional (triage signal); writing zero lots is intentional (we have no parser).
- **HiBid keeps its own discovery path.** `discover_vehicle_lots` is a cross-auction GraphQL query — much more efficient than per-auction page scraping. Don't force HiBid into the generic `discover_auctions → fetch_auction → fetch_lots → fetch_lot` shape just for uniformity.

## Design

Three ingestion strategies, all run from the existing `ingester` systemd timer under the same advisory lock:

```
ingester.main():
  acquire singleton lock 'ingester'
  for each source-strategy in (hibid_lot_first, mcdougall_lot_first, fag_router):
    try:
      strategy()                   # isolated failure
    except Exception:
      log.exception(...)           # don't abort siblings
  release lock
```

### Strategy A — HiBid lot-first (existing, untouched)

`HibidSource.discover_vehicle_lots(province)` for each province. Writes auctions + lots + NOTIFY enrichment_pending. **No code change.** Just moves into a named function so the dispatch loop is readable.

### Strategy B — McDougall lot-first (new)

Mirrors Strategy A's shape because McDougall has the same cross-auction catalog affordance:

```
McDougallSource.discover_vehicle_lots():
  for page in 1..N:
    GET products.php?category=Vehicles&page=<page>&sort=closing_asc
    for each lot card on page:
      parse summary (year/make/model/end_time/current_bid/lot_url + auction_url)
      GET lot_url for detail (mileage/VIN/photos/description)
      yield (RawAuction, RawLot)
```

Same downstream wiring as HiBid: `upsert_auction → upsert_lot_with_status_cascade → NOTIFY enrichment_pending`. Enricher, valuator, notifier are platform-agnostic and just consume the lot rows.

### Strategy C — FAG router (discovery signal only)

```
FarmAuctionGuideSource.discover_auctions() yields AuctionRef where ref.source is:
   - "hibid"            → upsert auction only. lots come from Strategy A.
   - "mcdougall"        → upsert auction only. lots come from Strategy B.
   - "unknown:<host>"   → upsert auction only. surfaces in /needs-plugin.
```

For every yielded ref we call `upsert_auction(ref, discovered_via="fag")` and nothing else. No lot fetching here — HiBid lots are owned by Strategy A, McDougall lots by Strategy B, and `unknown:` has no parser (by design). FAG is a pure discovery signal in this design.

### Dedup posture

`upsert_auction` is idempotent on `(source, source_auction_id)`. Three convergence cases worth being explicit about:

| Same auction discovered via | Dedup key | Outcome |
|---|---|---|
| Strategy A (HiBid GraphQL) + Strategy C (FAG router) | `("hibid", <hibid-numeric-id>)` | One row. **Assumes** FAG-derived HiBid URLs surface the same numeric ID HiBid GraphQL uses. Verify on first run. |
| Strategy B (McDougall catalog) + Strategy C (FAG router) | `("mcdougall", <GUID>)` | One row. GUID extracted from `auction-event.php?arg=<GUID>` is the same string in both flows. |
| FAG-routed `unknown:foo.com` rediscovered next sweep | `("unknown:foo.com", <last-path-segment>)` | One row (idempotent). |

If a HiBid URL on a FAG province page lacks the catalog numeric ID, the FAG router's `resolve_platform` already returns `None` (skip) rather than emitting a malformed ref. So the dedup risk is bounded.

### Bid poller dispatch

Currently HiBid-only. After McDougall lands as a real fetcher, the bid-poller needs to route lots to the right plugin:

```
poller.main():
  for each pending lot:
    source = lot.source
    plugin = SOURCES[source]
    if not isinstance(plugin, BidPoller):
      log.warning("no bid-poller for source", source=source); skip
    observation = await plugin.poll_bid(lot_ref)
    persist...
```

The current `poller.py` already imports HiBid concretely; this is a small refactor to look up by `lot.source` from `SOURCES`. Same shape as ingester dispatch.

## Observability

Per-strategy structured logs:
```
log.info("ingest strategy start", strategy="hibid_lot_first")
log.info("ingest strategy done", strategy="hibid_lot_first",
         auctions_upserted=N, lots_upserted=M, duration_s=...)
log.info("ingest strategy start", strategy="fag_router")
log.info("ingest strategy done", strategy="fag_router",
         auctions_upserted=N,
         by_platform={"hibid": 12, "mcdougall": 3, "unknown:foo.com": 1})
```

The `by_platform` breakdown is the new diagnostic — it answers "what's actually in our funnel" with one log line and feeds the implicit prioritization of future plugin work.

## Implementation breakdown (intended commit shape)

Ordered so every commit leaves the tree green and passing tests. Each is a single logical change reviewable on its own. Schema first because the McDougall plugin depends on it.

1. **Schema: buyer-premium cap/floor** — Alembic revision adds `auctions.buyer_premium_max_cad NUMERIC(10,2) NULL` and `buyer_premium_min_cad NUMERIC(10,2) NULL`. ORM model + RawAuction dataclass updated. HiBid plugin explicitly passes `None/None`. Existing FAG router stub also `None/None`. (small-medium)
2. **Scoring: cap/floor in all_in_cost** — Update `scoring/score.py::all_in_cost` to accept optional `buyer_premium_max_cad`/`min_cad` and clamp the premium. Update `recommended_max_bid` for the piecewise inverse (analytic, not iterative — three regimes: floored, linear, capped). Update `price_deal_score`, `valuator.py` call sites. Add unit tests for: no-cap (regression on existing math), capped at high bid, floored at low bid, boundary conditions. (medium)
3. **Refactor ingester seam** — Extract HiBid path into `_run_hibid_lot_first()`. No behavior change; sets the dispatch shape. (small)
4. **Fix FAG `PLATFORM_RULES` McDougall regex** — Change `/auction/(\d+)` to match `auction-event.php?arg=<GUID>`. Stub plugin's `_MCDOUGALL_AUCTION_URL` regex updated to match. Existing FAG unit tests get an updated McDougall fixture. (small)
5. **Add McDougall catalog walker** — `McDougallSource.discover_vehicle_lots()` paginates `products.php?category=Vehicles`, yields `(RawAuction, RawLot)` summary rows. Sets cap/floor 2000/20. Fixture-captured test of one page. (medium)
6. **Add McDougall lot-detail parser** — `fetch_lot()` parses mileage / VIN / photos / description from a detail-page fixture. (medium)
7. **Add McDougall `poll_bid`** — Fetches lot URL, extracts current_high_bid + end_time + status. Fixture test with OPEN and CLOSED states. (small)
8. **Wire McDougall lot-first into ingester** — `_run_mcdougall_lot_first()` mirroring `_run_hibid_lot_first()`. Per-source `_PARSER_VERSION`. (small)
9. **Wire FAG router strategy into ingester** — `_run_fag_router()` upserts auctions only, accumulates `by_platform` counts. (small)
10. **Wire all three strategies into `main()`** — Per-strategy try/except, structured logs with `by_platform` diagnostic. (small)
11. **Bid-poller dispatch refactor** — Look up plugin via `SOURCES[lot.source]` instead of importing HiBid directly. Falls through with a warning for unknown sources. (small)
12. **Manual production verification** — Run ingester once. Eyeball logs. Query `auctions` grouped by source. Curl `/needs-plugin`. Spot-check 3 McDougall lots end-to-end (enriched → valued → notification fired if it qualifies). Confirm at least one capped-premium lot computes correctly. Documented in this spec's appendix. (manual gate, no commit unless something broke)

Commits 5-7 each include fixture capture from the live McDougall site checked into `tests/sources/mcdougall/fixtures/`. Fixtures are intentionally small (one page each) since we want the test to assert structure, not coverage.

## Verification path (so we know it works)

```
# Pre-run snapshot
docker exec carbuyer-pg psql -U carbuyer -d carbuyer -c \
  "SELECT source, COUNT(*) FROM auctions GROUP BY source ORDER BY source;"

# Run ingester once
systemctl start carbuyer-ingester.service
journalctl -u carbuyer-ingester.service -f --since "2 minutes ago"

# Post-run snapshot — expect hibid count unchanged or +N (new discoveries),
# new rows for mcdougall and any unknown:<host> platforms, zero HiBid
# auctions written via FAG that don't already exist via Strategy A.
docker exec carbuyer-pg psql -U carbuyer -d carbuyer -c \
  "SELECT source, COUNT(*) FROM auctions GROUP BY source ORDER BY source;"

# Confirm dashboard surfaces them
curl -s http://localhost:8000/needs-plugin | grep -oE 'source="[^"]+"' | sort -u
```

Pass criteria: ≥1 row each in mcdougall + at least one `unknown:<host>` source after the first FAG run; no duplicate HiBid rows; `/needs-plugin` lists every unknown host once.

## Risks

- **FAG page structure changes silently.** The router uses CSS selectors against AB/SK/MB/BC pages. If they change, we go from "discovers everything" to "discovers nothing" with only a log warning. Mitigation: alert if a per-province sweep yields zero `AuctionRef`s when previous run yielded non-zero. Out of scope for this design; add to backlog if it bites.
- **Auction volume could spike.** First FAG run might discover 100+ auctions across the long tail. All metadata-only, so cheap, but the dashboard `/needs-plugin` view will get noisy. Acceptable for first iteration; cluster/group by host if it gets unwieldy.
- **HiBid dedup assumes matching `source_auction_id`.** If FAG-derived HiBid URLs have a different ID format than direct HiBid GraphQL, we get duplicate auction rows. Verification step in the implementation breakdown catches this; mitigation if needed is a small normalization helper in the HiBid plugin.
- ~~**McDougall buyer-premium is capped, not flat percent.**~~ Resolved 2026-05-16: cap/floor columns added in commit #1; scoring math updated in commit #2. McDougall sets max=2000/min=20; HiBid sets None/None (linear regime collapses to existing math). `recommended_max_bid` becomes piecewise; tested across all three regimes.
- **McDougall HTML may change.** Selectolax-based parser is fragile to site redesigns. Mitigation is fixture-based tests so we notice during dev, plus the existing `parser_version` cascade re-pending mechanism for production drift.
- **McDougall bid-poll volume.** 147 vehicles in catalog today × poll cadence × number of services = needs to fit inside the rate budget. HiBid is currently the volume floor; doubling sources doubles request count. Recommend an early `journalctl` check after first day to confirm no rate-limit fallout. Defer rate-limit work unless we observe it.

## Open follow-ups (post this PR)

- **bid_poller doesn't poll McDougall lots.** Production check (2026-05-18) showed `with_bid=0/147` for McDougall vs 76/321 for HiBid. Root cause: `_build_raw_auction` leaves `auctions.scheduled_end_at` NULL because the lot-detail page doesn't expose auction-level dates. `_load_open_lot_refs` orders by `auction.scheduled_end_at ASC NULLS LAST` and caps at 200; HiBid's 321 non-NULL-dated lots fill the batch and McDougall never gets polled. Fix candidates: (a) change poller to use `coalesce(auction.scheduled_end_at, lot.scheduled_end_at)` (cleaner — admits that "the lot is what we poll"); (b) accumulate `max(lot.scheduled_end_at)` per-auction-GUID in McDougall's `discover_vehicle_lots` and set it on the auction. Tracked as a separate work item.
- Per-source poll cadence tuning if rate-limits or stale-data bite.
- McDougall closed-lot fixture + OPEN→CLOSED detector (commit #7 deferred this; today the bid_poller's force-close-by-scheduled-end guard handles transitions).
- McDougall live (in-room) auctions if those ever overlap our interest.

## Appendix A — FAG strategy dropped post-validation (2026-05-16)

The first production ingest after shipping commits #1-11 surfaced two
distinct failure modes on `fag_router`:

**1. URL structure changed.** The historical `/canada/<province>/` per-
province aggregator pages now 404. Their replacements live at
`/<province>-auctions` (e.g. `/alberta-auctions`).

**2. Cloudflare bot challenge on the new endpoints.** A `curl`/`httpx`
GET against `/alberta-auctions` returns a Cloudflare "Just a moment..."
interactive challenge page, not real HTML. Solving it requires a real
browser (Playwright/headless Chrome). The homepage and individual
auction detail pages still serve directly to httpx — only the per-
province browse pages are gated.

**3. Even past Cloudflare, the role has changed.** Old FAG: per-province
pages aggregated outbound links to platform sites (`hibid.com/.../catalog/X`,
`mcdougallauction.com/auction/X`). The router's premise was extracting
those platform URLs and routing them. New FAG: each auction is hosted
natively at `/<province>-auctions/<slug>.html` and links out to the
*auctioneer's own website* (e.g. `allenolsonauction.com`), not a
platform. So most outbound links would be `unknown:<auctioneer-host>`
even with full access — small custom sites we don't have plugins for.

**Decision.** Long-tail auctioneer discovery is fundamentally a human-in-
the-loop concern (the value is *"which auctioneers should I plug?"*, not
*"give me more lots automatically"*). Moved to a manual operator workflow
that walks FAG + sister sites
periodically and emits a markdown report of newly-seen auctioneer hosts
cross-referenced against the registered plugin list. Operator reviews
the report and decides which auctioneers warrant a new plugin
(McDougall-style). The skill tolerates site redesigns gracefully: when
FAG changes their HTML, the skill just describes the new structure to
the operator; an automated parser silently breaks.

`_run_fag_router` was deleted from the ingester. `FarmAuctionGuideSource`
stays in tree (still self-registers via the discoverer worker's import)
so the skill can reuse parsing helpers if useful.

## Appendix B — McDougall VIN parsing fix (2026-05-16)

First production run crashed on lot `6C95BAB0-9274-4320-B779-466296D3CD14`
(boat with trailer + motor). Its "Serial Number" field contained three
labeled IDs (`"Trailer: 5KTBS1810LF528753  Boat: BLBX2184J920 Motor:
2B721768"`, 59 chars). Cramming that into `auction_lots.vin`
(varchar(32)) raised `StringDataRightTruncation` and the dispatch
try/except correctly contained it to the McDougall strategy — HiBid's
111 lots still ingested cleanly.

Fix: `_parse_vin` now applies a single-VIN shape filter
(11-17 chars of `[A-HJ-NPR-Z0-9]`, the VIN-standard alphabet that
excludes I/O/Q). Non-VIN-shaped values go to `extras["raw_serial_number"]`
for human inspection, `vin` stays `None`. A regression test pins the
exact production-crash string.
