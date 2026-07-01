# Days-on-Market Tracking — Design & Spec

**Date:** 2026-06-30
**Status:** Approved — implementing
**Phase:** Phase 2 (want-list pivot), design-doc §5d — the last Phase-2 feature
**Depends on:** Kijiji private source + `previous_asking`/price-drop work + tiered delivery (all merged to `main`)

---

## TL;DR

Surface **buyer leverage** on private-listing matches: how long a listing has sat and how much its
price has fallen. A listing up 90 days that has dropped $3,000 across 2 cuts is a negotiating
signal, not urgency. Two additive columns on `PrivateListing` (`original_asking_price_cad`,
`price_drop_count`) plus the existing `asking_price_cad` / `previous_asking_price_cad` /
`days_on_market` / `first_seen_at` give everything the leverage line needs — no new table. One pure
helper builds one plaintext line shown on both the Discord want-match alert and the dashboard
want-detail page.

---

## 1. Goal & non-goals

**Goal:** give the buyer negotiating context on private matches — days-on-market + a price-drop
summary — wherever a private match is surfaced.

**Non-goals (YAGNI):**
- No full price-observation history (a table or JSONB trail) — the compact summary (original →
  current, drop count, days) conveys the leverage.
- No chart / timeline.
- No price-*increase* tracking beyond "an increase is not a drop."
- Auctions are unaffected (leverage is a private-sale, fixed-price concept).

**Success criteria:** a private match shows a correct leverage line ("listed 90 days · down $3,000
(17%) from $18,000 · 2 drops") on both surfaces; days-on-market is present even when the source
omits it; a listing that never dropped shows just "listed N days"; auctions show nothing new.

## 2. Schema — two additive columns (one migration)

`PrivateListing` gains:
- `original_asking_price_cad: Decimal | None` — the first-seen asking price (set once, at insert).
- `price_drop_count: Mapped[int]` with `default=0, server_default=text("0"), nullable=False`.

Additive + defaulted, so existing rows validate/backfill cleanly. A standard Alembic migration adds
both (down-migration drops them).

## 3. Population — `db/upserts.py::upsert_private_listing`

Reuse the function's existing insert path and price-change/drop branch (it already records
`previous_asking_price_cad` and clears `notified_at` on a drop):

- **Insert** (new listing): `original_asking_price_cad = raw.asking_price_cad`, `price_drop_count = 0`.
- **Drop** (`asking < pre_asking`, the branch that sets `previous_asking` + reopens want matches):
  `price_drop_count += 1`. If `original_asking_price_cad is None` (a row created before this feature),
  backfill it from `pre_asking` so old listings still get a baseline on their next drop.
- **Increase**: records `previous_asking` (unchanged behavior); does **not** touch `price_drop_count`
  or `original_asking_price_cad`.

## 4. Days-on-market — prefer source, fall back to computed

`wants/leverage.py`:
```python
def effective_days_on_market(
    days_on_market: int | None, first_seen_at: datetime | None, now: datetime,
) -> int | None:
    if days_on_market is not None:
        return days_on_market            # source value = the seller's true listing age
    if first_seen_at is not None:
        return max(0, (now - first_seen_at).days)
    return None
```
Prefer the source value (a listing may have been posted long before we discovered it); fall back to
our own `first_seen_at`; None when we have neither.

## 5. The leverage line — one pure function, both surfaces

`wants/leverage.py`:
```python
def buyer_leverage_line(offer: VehicleOffer, now: datetime) -> str | None:
    """A compact buyer-leverage line for a PRIVATE listing, or None (auctions / no data)."""
```
Behavior (only for `PrivateListing`; returns None otherwise):
- Compute `dom = effective_days_on_market(offer.days_on_market, offer.first_seen_at, now)`.
- Build clauses:
  - `listed {dom} days` when `dom is not None`.
  - drop clause when `original_asking_price_cad` and current `asking_price_cad` are known and
    `original > current`: `down ${orig-cur:,} ({pct}%) from ${orig:,}` where `pct = round((orig-cur)/orig*100)`;
    append `· {n} drops` when `price_drop_count` ≥ 1 (`1 drop` singular).
- Join present clauses with ` · `. Return None when no clause is present (no dom, no drop).

One plaintext line, reused verbatim by Discord and rendered as text on the dashboard — no second
render path.

## 6. Surfacing

**Discord** — `bot/messages.py` `LotEmbedData` gains `leverage_line: str | None = None`; the
notifier's `_embed_data` sets it via `buyer_leverage_line(lot, now)` (it already threads `now`);
`render_want_match_text` appends the line (its own paragraph) when present. Auction embeds pass None.

**Dashboard** — `want_detail` router computes `buyer_leverage_line(item.lot, now)` per item and passes
`leverage` into each item dict; `want_detail.html` renders it under the match (muted text) when
present. (`want_detail` already selects the polymorphic `VehicleOffer`, so private listings arrive
with the new fields.)

## 7. Reuse / Extend / New

**Reuse:** `upsert_private_listing`'s drop detection + `previous_asking`; `first_seen_at`;
`VehicleOffer.offer_price`; the `want_detail` polymorphic query; the `render_want_match_text`
paragraph pattern; the notifier `_embed_data` `now`.

**Extend:** `db/models.py` (+2 cols); `db/upserts.py` (populate); `bot/messages.py`
(`LotEmbedData.leverage_line` + render); notifier `_embed_data`; `dashboard/routers/wants.py`
`want_detail` + `want_detail.html`.

**New:** `wants/leverage.py` (`effective_days_on_market` + `buyer_leverage_line`); one Alembic
migration.

## 8. Testing (TDD)

- `effective_days_on_market`: source present → source; source None + first_seen → computed days;
  both None → None; negative clamp.
- `buyer_leverage_line`: full (dom + drop + count); no drops → just `listed N days`; auction offer →
  None; no data → None; `original_asking` NULL → drop clause omitted; 1 drop singular.
- `upsert_private_listing`: insert sets `original` + `price_drop_count=0`; a drop increments the count
  + backfills `original` when NULL; an increase leaves count/original untouched.
- Migration: throwaway-DB up/down round-trip (columns added then dropped); backward-compat.
- Discord: `render_want_match_text` includes the leverage line for a private listing, omits it for an
  auction / when None.
- Dashboard: `want_detail` for a private-listing match renders the leverage line.

## 9. Build sequence

1. `wants/leverage.py` `effective_days_on_market` + `buyer_leverage_line` (+ pure tests).
2. `PrivateListing` +2 columns + Alembic migration (throwaway-DB round-trip gate).
3. `upsert_private_listing` population (insert / drop-increment-and-backfill / increase) + tests.
4. Discord: `LotEmbedData.leverage_line` + `_embed_data` + `render_want_match_text` + tests.
5. Dashboard: `want_detail` router + `want_detail.html` + test.

Each step compiles + tests green before the next.

## 10. Risks

- **Source `days_on_market` accuracy** — trusted as-is; a wrong source value shows a wrong DOM. Low
  stakes (informational); the computed fallback covers omissions.
- **`original_asking` for pre-feature rows** — NULL until their next drop backfills it; until then the
  drop clause is omitted (graceful). Acceptable — no backfill migration needed.
- **Additive migration** — `price_drop_count` NOT NULL with `server_default=0` so existing rows get 0;
  `original_asking_price_cad` nullable. No data migration.
