# US / Cross-Border Auction Expansion — Design

**Status:** draft for review
**Author:** Mark
**Date:** 2026-07-02

## 1. Goal

Let the assistant surface and *correctly value* US auction inventory for a
Canada-based buyer. Today every source and cost model assumes Canadian
locations and CAD prices; a US lot would be mis-valued and never matched.

## 2. Why (what recon found)

- Canadian auction houses concentrate on **HiBid**, which we already aggregate
  Canada-wide. The largest *incremental* inventory is **US**: HiBid's US states
  and **Proxibid** (5,591 vehicle lots, US-dominated, curl_cffi works after the
  #31 fix). Proxibid has **no geo filter** and a thin Canadian slice — but as a
  *US* source it's valuable.
- Using US lots requires three things that don't exist yet: currency
  conversion, cross-border geography/matching, and an import-adjusted landed
  cost. Everything else (enrichment, dedup, alerting, deal scoring) is
  location-agnostic and works unchanged.

## 3. Current Canada-only assumptions (grounded in code)

| Area | File | Assumption |
|------|------|-----------|
| Landed cost | `scoring/landed_cost.py` | province↔province capital distances + per-province inspection fees; **no duty / RIV / FX / US distances** |
| Want geo gate | `wants/matcher.py::_province_ok` | matches only when `pickup_province ∈ criteria.provinces` |
| Want model | `wants/criteria.py` + `shared/config.py::Province` | `provinces` is a Canadian `Literal` |
| Currency | `db.models` / upserts | prices stored as CAD (`asking_price_cad`, `current_high_bid_cad`); US sources are USD |

## 4. The cross-border cost model (the core new piece)

A new `us_import_premium(*, price_cad, origin_state, home) -> Decimal` mirroring
`landed_cost_premium`, adding to the CAD purchase price:

- **Currency:** USD → CAD at an FX rate (see §6).
- **Duty:** 0% for USMCA-qualifying vehicles (most US-sold vehicles built in
  North America); 6.1% otherwise. *Coarse default: assume 0% and flag "verify
  origin".* Modelling per-VIN origin is out of scope for phase A.
- **Federal GST 5%** on the CAD import value; **RIV fee** (~$325 + GST);
  **A/C excise** ($100); green levy only for rare gas-guzzlers.
- **Cross-border transport:** origin-state → home distance × per-km. Extend the
  distance model with US-state centroids, or a coarse zone table (border-state
  vs interior).
- **Admissibility:** some US vehicles are inadmissible to Canada (Transport
  Canada list). Phase A: label US lots "verify admissibility"; phase B: a
  checklist / known-inadmissible filter.

Output is an *estimate*, always surfaced as such next to the deal.

## 5. Geography & want model

- Introduce a location dimension spanning CA provinces + US states. Recommended:
  add `include_us: bool` (+ optional `us_states: list[str]`) to `WantCriteria`,
  and generalize `_province_ok` → `_location_ok` that admits a US-state pickup
  when the want opts in. Keeps the Canadian `provinces` semantics intact;
  US is strictly opt-in per want.
- Lots carry a `pickup_country` (CA/US) derived at ingest from the source +
  location code, so matching and landed-cost pick the right cost path.

## 6. Currency

- Tag each source with a `source_currency` (Proxibid/HiBid-US = USD, everything
  else = CAD). Convert USD → CAD at ingest so downstream scoring stays CAD-only.
- FX rate: **phase A** a config `USD_CAD_RATE` (manually refreshed); **phase B**
  a lightweight Bank of Canada valet API fetch cached daily.

## 7. Sources (sequence)

1. **HiBid US states** — extend `PROVINCE_PATH` to a region map incl. US state
   slugs (`hibid.com/montana/…`); config type widens from `Province` to a
   region code. Biggest US inventory, cheapest change, mirrors the Canada-wide
   expansion already shipped.
2. **Proxibid** — full auction-source build over curl_cffi; make/model search,
   client-side geo filter on the `"City, ST"` suffix (no server geo filter).
3. *(later)* GovDeals US (gov surplus), Copart/IAA (US salvage).

## 8. Phasing

- **Phase A — coarse surface (ship fast):** `source_currency` + USD→CAD config
  rate; `include_us` want flag + geo gate; approximate `us_import_premium`
  (duty=0 USMCA + GST + RIV + FX + coarse transport), labeled an estimate;
  HiBid US states. Outcome: US deals visible and roughly costed.
- **Phase B — proper model:** live FX, US-state distance table, RIV/tax
  precision, admissibility filter, Proxibid, origin-aware duty.

## 9. Non-goals

- Per-VIN USMCA origin determination (phase A assumes 0% duty).
- US salvage/rebuild sources (Copart/IAA) — separate later decision.
- Automated customs brokerage / paperwork — the buyer handles import execution.

## 10. Decisions needed

1. **FX:** config rate (phase A) vs live Bank-of-Canada fetch now?
2. **Want model:** `include_us` boolean vs an explicit `us_states` list?
3. **Duty:** assume USMCA-0 (coarse) vs model origin?
4. **Confirm start:** Phase A, beginning with HiBid US-states + the currency +
   geo-gate slices?
