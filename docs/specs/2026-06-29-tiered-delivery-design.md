# Tiered Delivery — Design & Spec

**Date:** 2026-06-29
**Status:** Approved — ready for implementation plan
**Phase:** Phase 2 (want-list pivot), design-doc §5d
**Depends on:** want-list spine (Phase 0), `vehicle_offer` split (Phase 1), WG5 flipper teardown + archetype expander (PR #25, merged to `main` as `d04b99f`)

---

## TL;DR

Today the notifier fires **every** want-match alert instantly. Split delivery into two tiers: an
**instant** Discord ping for genuinely good matches (≥ a deal threshold below market, a price-drop,
or an auction closing within ~24h), and **one daily digest** for everything else (ordinary
under-budget matches). Re-introduce quiet hours for the instant tier — a quiet-window match is
simply deferred and swept into the next morning's digest. The state rides the existing
`want_matches.notified_at` ledger, so there is **no migration**: the only question is *who*
delivers an un-notified match — the notifier (instant) or the nightly digest job (everything else).

---

## 1. Goal & non-goals

**Goal:** reduce alert noise. The owner gets interrupted only for matches worth acting on now; the
long tail of ordinary in-budget matches arrives as one scannable daily summary.

**Non-goals (YAGNI):**
- No per-want digest scheduling — one global daily digest.
- No separate digest Discord channel — the digest posts to the existing `wants` channel.
- No new `delivery_tier` DB column — tier is a pure function of existing fields.
- No closing-soon quiet-hours override — the kept auction `closing_soon` trigger already handles
  the genuinely time-critical T-1h reminder for *watched* lots, separately from want-match alerts.

**Success criteria:** a great-deal / price-drop / closing-soon match pings immediately (outside quiet
hours); an ordinary match never pings and instead appears in the next daily digest exactly once;
quiet-window instant matches roll into the digest; nothing double-delivers.

## 2. The classifier — `wants/delivery.py`

One pure function, the single source of truth for the tier, reused by all three consumers:

```python
def delivery_tier(
    *,
    want_relative_score: float | None,
    offer_price_cad: Decimal | None,
    previous_asking_price_cad: Decimal | None,
    scheduled_end_at: datetime | None,
    now: datetime,
    deal_threshold: float,
    closing_hours: int,
) -> Literal["instant", "digest"]:
```

`instant` when **any** of:
- `want_relative_score is not None and want_relative_score >= deal_threshold` (a standout deal), OR
- `previous_asking_price_cad is not None and offer_price_cad is not None and previous_asking_price_cad > offer_price_cad` (a price-drop on an already-matched listing), OR
- `scheduled_end_at is not None and timedelta(0) <= scheduled_end_at - now <= timedelta(hours=closing_hours)` (an auction lot closing soon).

Otherwise `digest`. No DB, no settings import (inputs injected) — trivially unit-testable.

## 3. State model — reuse `notified_at`, no migration

`want_matches.notified_at` already means "delivered (fire-once)". Tiering only changes *who* stamps it:

| Match state | Meaning | Delivered by |
|---|---|---|
| `notified_at IS NULL`, instant-tier, outside quiet hours | ready to ping now | **notifier** (instant) |
| `notified_at IS NULL`, instant-tier, in quiet hours | deferred | **digest** (next run) |
| `notified_at IS NULL`, digest-tier | waiting for the daily summary | **digest** |
| `notified_at IS NOT NULL` | delivered (either tier) | — |

The notifier and the digest job both stamp `notified_at`; a match is delivered exactly once. The
existing price-drop re-alert (which clears `notified_at`) re-enters this table as instant-tier and
re-pings — consistent with the rule.

## 4. The three consumers of `delivery_tier`

**(a) Valuator — `_has_unnotified_want_match` → instant-only.** Today the valuator forces
`notification_status = PENDING` whenever *any* un-notified match exists. If digest-tier matches did
that, the lot would churn PENDING↔SKIPPED on every re-valuation (the notifier would defer them) — the
same class of churn fixed in the PR review. So the gate becomes "is there an un-notified, non-
dismissed, **enabled**, **instant-tier** match?" Digest-tier matches do not force `PENDING`; they wait
for the cron digest. (The enabled-join was added in the PR-review fixes; keep it.)

**(b) Notifier — `_post_want_alerts` → instant-only, quiet-hours aware.** For each un-notified want
alert, compute its tier; post + stamp `notified_at` only when `tier == "instant"` **and** not
`_in_quiet_hours(now)`. A `digest`-tier or quiet-deferred match is left un-notified (the notifier
does not stamp it). The lot's `notification_status` bookkeeping is unchanged; it is decoupled from the
per-match `notified_at`.

**(c) Digest job — delivers everything still un-notified** (see §5).

## 5. Digest job — new cron app `apps/digest/`

Mirrors `auction_distiller` exactly: a cron entry point (`apps/digest/__main__.py`), no `LISTEN`, no
`claim`, single-instance, runs once and exits. Crash recovery is free — the next nightly run re-reads
whatever is still un-notified.

```
async def main(now: datetime | None = None) -> None:
    # 1. SELECT WantMatch ⋈ Search ⋈ VehicleOffer
    #    WHERE notified_at IS NULL AND dismissed = False AND Search.enabled = True
    # 2. group by want; render ONE message (render_digest_text)
    # 3. post to the 'wants' channel via discord_post
    # 4. on success, stamp notified_at = now for every match in the digest
    # 5. if there are zero matches → log "nothing to digest" and exit (no empty post)
```

It delivers both digest-tier matches **and** quiet-deferred instant matches (everything still
un-notified is, by definition, the daily backlog). The operator schedules it at a morning hour
(cron/systemd timer) — the same deployment story as the distiller, which is already a nightly cron.

Atomicity: post first, then stamp `notified_at` in a fresh transaction (mirrors the notifier's
post-then-stamp ordering, so a stamp failure can't hide a delivered digest, and a post failure leaves
the matches un-notified for the next run).

## 6. Quiet hours (re-add what WG5 removed)

Re-introduce `_in_quiet_hours(now, start_hour, end_hour)` (the exact wraparound-window helper WG5
deleted; it had unit tests worth restoring) and the `quiet_hours_start` / `quiet_hours_end` settings.
Only the **notifier's instant want-alert path** consults it. A quiet-window instant match is left
un-notified → the next digest delivers it. This *is* the §5d "quiet hours → morning digest" behavior,
with no dedicated deferral state. The digest job itself runs at a non-quiet hour (operator-scheduled)
and does not check quiet hours.

`_in_quiet_hours` is re-added to `notifier.py` (its only consumer — restore the exact helper + its
two unit tests WG5 deleted).

**Schedule the digest at `quiet_hours_end`.** Running the daily digest at the moment quiet hours end
(default 08:00) means a quiet-window instant match — a great deal that arrived overnight — is the
first thing delivered in the morning, rather than waiting an arbitrary number of hours. The operator
points the cron/timer at that hour.

## 7. Channel & content

- **Instant** → the `wants` channel (unchanged `render_want_match_text`).
- **Digest** → the same `wants` channel, one grouped message via a new `render_digest_text(groups)`:
  for each enabled want with matches, the want name and a compact line per vehicle (year/make/model,
  price, % below market, link). Kept short; deep-links out. (A dedicated digest channel is a trivial
  later change — out of scope.)
- Per-want mute (`Search.enabled`) and cross-source VIN dedup already gate both tiers — no new work.

## 8. Settings (`shared/config.py` + `.env.example`)

- `instant_deal_threshold: float = 0.15` — want_relative_score at/above which a match pings instantly.
- `instant_closing_hours: int = 24` — auction lots closing within this window ping instantly.
- `quiet_hours_start: int = 22`, `quiet_hours_end: int = 8` — re-added; quiet window for instant pings.

## 9. Reuse / Extend / New

**Reuse as-is:** `want_matches.notified_at` ledger (no migration); the `auction_distiller` cron app
shape + its `__main__`/`run`/crash-recovery idiom; `notifier/discord_post`; `score_want_deal`
(produces `want_relative_score`); `channel_resolver`; `Search.enabled` mute; VIN dedup.

**Extend:**
- `valuator.py` `_has_unnotified_want_match` → instant-tier-only gate (uses `delivery_tier`).
- `notifier.py` `_post_want_alerts` (+ the `_process_one` call path that has the auction) → instant-
  only + quiet-hours defer.
- `bot/messages.py` → `render_digest_text`.
- `shared/config.py` + `.env.example` → the four settings above.

**Build new:**
- `wants/delivery.py` — `delivery_tier()` (the only genuinely new logic).
- `apps/digest/` — `__init__.py`, `__main__.py`, `digest.py` (the cron job).
- `notifier/quiet_hours.py` (or inline) — re-added `_in_quiet_hours`.

## 10. Testing (TDD)

- `delivery_tier` truth table: deal ≥ threshold → instant; below → digest; price-drop → instant;
  closing within window → instant; ordinary (none) → digest; boundary cases (exact threshold, exact
  closing window, None inputs lenient toward digest).
- `_in_quiet_hours`: restore the WG5 wraparound + non-wraparound unit tests.
- Valuator: an un-notified **digest-tier** match does NOT force `notification_status=PENDING`; an
  un-notified **instant-tier** match DOES (regression-pin the no-churn property).
- Notifier: posts an instant-tier match; does NOT post a digest-tier match (left un-notified); a
  quiet-window instant match is deferred (not posted, not stamped).
- Digest job: gathers un-notified non-dismissed enabled matches (incl. a quiet-deferred instant one),
  posts one grouped message, stamps `notified_at`; posts nothing + stamps nothing when empty; a
  post failure leaves matches un-notified.

## 11. Build sequence (for the plan)

1. `wants/delivery.py` `delivery_tier` + truth-table tests.
2. Re-add `_in_quiet_hours` + its tests; add the four settings.
3. Valuator instant-only gate (no-churn regression test).
4. Notifier instant-only + quiet-hours defer (post/defer tests).
5. `render_digest_text` + tests.
6. `apps/digest/` cron job + tests (gather/render/post/stamp/empty/failure), wired to `discord_post`
   + `channel_resolver` like the other workers.

Each step compiles + tests green before the next.

## 12. Risks

- **Digest job not scheduled** — if the operator never runs the cron, digest-tier + quiet-deferred
  matches never deliver (they sit `notified_at IS NULL` forever). Mitigation: document the cron/timer
  in the README/run docs alongside the distiller; the job is idempotent and cheap to over-schedule.
- **Quiet-hours clock** — `_in_quiet_hours` uses UTC hour-of-day (the WG5 helper's documented MVP
  limitation); a per-user local offset is a later refinement, not this scope.
- **Tier drift between valuator and notifier** — both must use the SAME `delivery_tier` with the same
  inputs, or a match could force PENDING (valuator thinks instant) yet be deferred (notifier thinks
  digest). The shared pure function + identical inputs (score, previous-asking, scheduled-end) prevent
  this; a test pins valuator/notifier agreement on a borderline case.
- **No migration, but a behavior change** — previously-instant ordinary matches now wait for the
  digest; acceptable and the whole point. No data migration needed.
- **Bounded quiet-hours re-processing** — an instant-tier match that is quiet-deferred stays
  `notification_status`-terminal (SKIPPED) with `notified_at IS NULL`; the morning digest is its
  recovery path (not a re-NOTIFY). A re-valuation during quiet hours (e.g. an overnight bid change)
  re-forces PENDING → the notifier re-defers → SKIPPED again. This is bounded by overnight
  re-valuation frequency and self-heals at quiet-hours-end / the morning digest; it is delivery
  churn, not a correctness bug, and never double-delivers (the single `notified_at` stamp guarantees
  once-only).
