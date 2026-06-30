# Tiered Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split want-match delivery into an instant Discord ping for genuinely good matches (great deal / price-drop / closing-soon) and one daily digest for everything else, with quiet hours.

**Architecture:** A pure `delivery_tier()` classifier is consulted in three places — the valuator's force-PENDING gate (instant-only, no churn), the notifier's want-alert post (instant-only + quiet-hours defer), and a new distiller-shaped nightly `apps/digest/` cron job that delivers everything still un-notified. Tier rides the existing `want_matches.notified_at` ledger — **no migration**.

**Tech Stack:** Python 3.13 asyncio, SQLAlchemy 2 async, Pydantic v2, aiohttp (Discord REST), the existing `_runner.run_worker` worker harness.

## Global Constraints

- Spec of record: `docs/specs/2026-06-29-tiered-delivery-design.md`.
- **No DB migration** — reuse `want_matches.notified_at` (delivered = stamped).
- `delivery_tier` is a PURE function — inputs injected, no DB, no `settings` import.
- The SAME `delivery_tier` with the SAME inputs must be used by the valuator and notifier (no tier drift).
- TDD: failing test first, run red, minimal implementation, run green, commit.
- Tests build schema via `Base.metadata.create_all`; DB tests use the `engine`/`session` fixtures in `tests/conftest.py`. `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` on bare async tests; the existing worker tests do use `@pytest.mark.asyncio` — match the file you edit).
- Run one test: `.venv/bin/python -m pytest <path>::<name> -v`. Full suite: `.venv/bin/python -m pytest -q`.
- Defaults (verbatim): `instant_deal_threshold = 0.15`, `instant_closing_hours = 24`, `quiet_hours_start = 22`, `quiet_hours_end = 8`.
- No AI attribution anywhere in code, commits, or docs.

---

### Task 1: `delivery_tier` classifier

**Files:**
- Create: `src/carbuyer/wants/delivery.py`
- Test: `tests/wants/test_delivery.py`

**Interfaces:**
- Produces: `delivery_tier(*, want_relative_score: float | None, offer_price_cad: Decimal | None, previous_asking_price_cad: Decimal | None, scheduled_end_at: datetime | None, now: datetime, deal_threshold: float, closing_hours: int) -> Literal["instant", "digest"]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/wants/test_delivery.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from carbuyer.wants.delivery import delivery_tier

NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)


def _tier(**over: object) -> str:
    base: dict[str, object] = dict(
        want_relative_score=0.0, offer_price_cad=Decimal("8000"),
        previous_asking_price_cad=None, scheduled_end_at=None,
        now=NOW, deal_threshold=0.15, closing_hours=24,
    )
    base.update(over)
    return delivery_tier(**base)  # type: ignore[arg-type]


def test_great_deal_at_or_above_threshold_is_instant() -> None:
    assert _tier(want_relative_score=0.15) == "instant"
    assert _tier(want_relative_score=0.30) == "instant"


def test_ordinary_below_threshold_is_digest() -> None:
    assert _tier(want_relative_score=0.14) == "digest"
    assert _tier(want_relative_score=0.0) == "digest"
    assert _tier(want_relative_score=None) == "digest"


def test_price_drop_is_instant() -> None:
    assert _tier(
        want_relative_score=0.0,
        previous_asking_price_cad=Decimal("9000"),
        offer_price_cad=Decimal("8000"),
    ) == "instant"


def test_price_increase_is_not_a_drop() -> None:
    assert _tier(
        previous_asking_price_cad=Decimal("8000"),
        offer_price_cad=Decimal("9000"),
    ) == "digest"


def test_closing_within_window_is_instant() -> None:
    assert _tier(scheduled_end_at=NOW + timedelta(hours=12)) == "instant"
    assert _tier(scheduled_end_at=NOW + timedelta(hours=24)) == "instant"


def test_closing_beyond_window_or_past_is_digest() -> None:
    assert _tier(scheduled_end_at=NOW + timedelta(hours=25)) == "digest"
    assert _tier(scheduled_end_at=NOW - timedelta(hours=1)) == "digest"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/wants/test_delivery.py -v`
Expected: FAIL — `ModuleNotFoundError: carbuyer.wants.delivery`.

- [ ] **Step 3: Implement**

Create `src/carbuyer/wants/delivery.py`:

```python
"""Delivery tier for a want-match: instant ping vs the daily digest.

A pure classifier (inputs injected — no DB, no settings) so the valuator's
force-PENDING gate, the notifier's instant post, and the digest job all agree on
which matches are instant. A match is instant when it is a standout deal, a
price-drop on an already-matched listing, or an auction lot closing soon.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal


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
    if want_relative_score is not None and want_relative_score >= deal_threshold:
        return "instant"
    if (
        previous_asking_price_cad is not None
        and offer_price_cad is not None
        and previous_asking_price_cad > offer_price_cad
    ):
        return "instant"
    if scheduled_end_at is not None and (
        timedelta(0) <= scheduled_end_at - now <= timedelta(hours=closing_hours)
    ):
        return "instant"
    return "digest"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/wants/test_delivery.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/wants/delivery.py tests/wants/test_delivery.py
git commit -m "feat(wants): delivery_tier classifier (instant vs digest)"
```

---

### Task 2: Re-add quiet hours + the four settings

**Files:**
- Modify: `src/carbuyer/apps/notifier/notifier.py` (add `_in_quiet_hours`)
- Modify: `src/carbuyer/shared/config.py`
- Modify: `.env.example`
- Test: `tests/apps/test_notifier_worker.py` (restore the two `_in_quiet_hours` tests)

**Interfaces:**
- Produces: `notifier._in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool`; settings `instant_deal_threshold: float`, `instant_closing_hours: int`, `quiet_hours_start: int`, `quiet_hours_end: int`.

- [ ] **Step 1: Add the settings** (no test — config defaults)

In `src/carbuyer/shared/config.py`, locate the line `quiet_hours_start`/`home_province` area (after `home_province: Province = "AB"`) and add:

```python
    instant_deal_threshold: float = 0.15
    instant_closing_hours: int = 24
    quiet_hours_start: int = 22
    quiet_hours_end: int = 8
```

In `.env.example`, under the `# --- Common knobs ---` block (after `LOG_LEVEL=INFO`), add:

```
INSTANT_DEAL_THRESHOLD=0.15
INSTANT_CLOSING_HOURS=24
QUIET_HOURS_START=22
QUIET_HOURS_END=8
```

- [ ] **Step 2: Write the failing tests** for `_in_quiet_hours`

Add to `tests/apps/test_notifier_worker.py` (restore what WG5 removed):

```python
def test_in_quiet_hours_wraparound_window() -> None:
    from carbuyer.apps.notifier.notifier import (  # pyright: ignore[reportPrivateUsage]
        _in_quiet_hours,
    )
    base = datetime(2026, 5, 13, tzinfo=UTC)
    assert _in_quiet_hours(base.replace(hour=22), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=2), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=7), 22, 8) is True
    assert _in_quiet_hours(base.replace(hour=8), 22, 8) is False
    assert _in_quiet_hours(base.replace(hour=12), 22, 8) is False
    assert _in_quiet_hours(base.replace(hour=21), 22, 8) is False


def test_in_quiet_hours_non_wraparound() -> None:
    from carbuyer.apps.notifier.notifier import (  # pyright: ignore[reportPrivateUsage]
        _in_quiet_hours,
    )
    base = datetime(2026, 5, 13, tzinfo=UTC)
    assert _in_quiet_hours(base.replace(hour=9), 9, 17) is True
    assert _in_quiet_hours(base.replace(hour=16), 9, 17) is True
    assert _in_quiet_hours(base.replace(hour=17), 9, 17) is False
    assert _in_quiet_hours(base.replace(hour=8), 9, 17) is False
```

(`UTC` and `datetime` are already imported in this test file.)

- [ ] **Step 3: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/apps/test_notifier_worker.py -k in_quiet_hours -v`
Expected: FAIL — `ImportError: cannot import name '_in_quiet_hours'`.

- [ ] **Step 4: Implement `_in_quiet_hours`** in `src/carbuyer/apps/notifier/notifier.py` (add near the other module-level helpers, above `_process_one`):

```python
def _in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """Quiet hours window wraps midnight when start > end (typical: 22..08).
    UTC hour-of-day (single-user MVP; a per-user local offset is a later refinement)."""
    h = now.hour
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour
```

- [ ] **Step 5: Run to verify pass + full config test**

Run: `.venv/bin/python -m pytest tests/apps/test_notifier_worker.py -k in_quiet_hours tests/shared/test_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/apps/notifier/notifier.py src/carbuyer/shared/config.py .env.example tests/apps/test_notifier_worker.py
git commit -m "feat(notifier): re-add quiet-hours helper + tiered-delivery settings"
```

---

### Task 3: Valuator force-PENDING gate → instant-tier only

**Files:**
- Modify: `src/carbuyer/apps/valuator/valuator.py`
- Test: `tests/apps/test_valuator.py`

**Interfaces:**
- Consumes: `delivery_tier` (Task 1); `settings.instant_deal_threshold`, `settings.instant_closing_hours` (Task 2).
- Produces: `_has_unnotified_instant_match(session, lot: VehicleOffer, *, scheduled_end_at: datetime | None, now: datetime) -> bool` (replaces `_has_unnotified_want_match`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/apps/test_valuator.py` (read the file's existing seeding helpers — `_seed_auction`, `_seed_lot`, `_seed_comps` — and mirror them; the test exercises `value_one` end-to-end and asserts on `notification_status`):

```python
@pytest.mark.asyncio
async def test_digest_tier_match_does_not_force_notification_pending(
    session: AsyncSession,
) -> None:
    """An ordinary (digest-tier) want match must NOT force notification PENDING —
    it waits for the daily digest, avoiding PENDING<->SKIPPED churn."""
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000] * 10)
    lot = _seed_lot(session, a, source_lot_id="d1", make="Nissan", model="Xterra", year=2012)
    lot.current_high_bid_cad = Decimal("9900")  # ~1% below a ~10000 EV → digest tier
    want = Search(name="xterra", config={"makes": ["Nissan"], "models": ["Xterra"]})
    session.add(want)
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    # A want match was created (it matches), but the deal is ordinary → digest tier,
    # so notification is NOT forced PENDING.
    assert lot.notification_status != NotificationStatus.PENDING


@pytest.mark.asyncio
async def test_instant_tier_match_forces_notification_pending(
    session: AsyncSession,
) -> None:
    """A standout-deal (instant-tier) want match forces notification PENDING."""
    a = _seed_auction(session)
    await session.flush()
    _seed_comps(session, [10000] * 10)
    lot = _seed_lot(session, a, source_lot_id="i1", make="Nissan", model="Xterra", year=2012)
    lot.current_high_bid_cad = Decimal("3000")  # deeply below EV → instant tier
    want = Search(name="xterra", config={"makes": ["Nissan"], "models": ["Xterra"]})
    session.add(want)
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.notification_status == NotificationStatus.PENDING
```

(Imports already present in the file: `Decimal`, `Search`, `NotificationStatus`, `value_one`, `AsyncSession`. If `Search` isn't imported, add it from `carbuyer.db.models`.)

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/apps/test_valuator.py -k "digest_tier or instant_tier" -v`
Expected: FAIL — the digest-tier lot is forced PENDING by the current unconditional gate.

- [ ] **Step 3: Implement** — replace `_has_unnotified_want_match` in `src/carbuyer/apps/valuator/valuator.py`.

Add imports at the top:
```python
from datetime import UTC, datetime
from carbuyer.wants.delivery import delivery_tier
```
(`Search` is already imported from the PR-review fix.)

Replace the function:
```python
async def _has_unnotified_instant_match(
    session: AsyncSession,
    lot: VehicleOffer,
    *,
    scheduled_end_at: datetime | None,
    now: datetime,
) -> bool:
    """True if the lot has an un-notified, non-dismissed, enabled want match that
    is INSTANT-tier. Digest-tier matches are delivered by the nightly digest, so
    they must not force notification PENDING (would churn PENDING<->SKIPPED)."""
    rows = (
        await session.execute(
            select(WantMatch.want_relative_score)
            .join(Search, Search.id == WantMatch.search_id)
            .where(
                WantMatch.lot_id == lot.id,
                WantMatch.notified_at.is_(None),
                WantMatch.dismissed.is_(False),
                Search.enabled.is_(True),
            )
        )
    ).all()
    previous_asking = (
        lot.previous_asking_price_cad if isinstance(lot, PrivateListing) else None
    )
    return any(
        delivery_tier(
            want_relative_score=score,
            offer_price_cad=lot.offer_price,
            previous_asking_price_cad=previous_asking,
            scheduled_end_at=scheduled_end_at,
            now=now,
            deal_threshold=settings.instant_deal_threshold,
            closing_hours=settings.instant_closing_hours,
        ) == "instant"
        for (score,) in rows
    )
```

Update the single call site in `value_one` (search for `_has_unnotified_want_match`). It currently reads roughly:
```python
    if await _has_unnotified_want_match(session, lot.id):
        lot.notification_status = NotificationStatus.PENDING
```
Replace with (the `auction` local is already loaded earlier in `value_one` for auction lots):
```python
    scheduled_end = auction.scheduled_end_at if isinstance(lot, AuctionLot) and auction is not None else None
    if await _has_unnotified_instant_match(
        session, lot, scheduled_end_at=scheduled_end, now=datetime.now(UTC),
    ):
        lot.notification_status = NotificationStatus.PENDING
```
(`auction` is in scope: `value_one` loads it near the top with `auction = await session.get(Auction, lot.auction_id) if isinstance(lot, AuctionLot) else None`, and the `_has_unnotified_*` call is near the end of the same function.)

- [ ] **Step 4: Run to verify pass + full valuator file**

Run: `.venv/bin/python -m pytest tests/apps/test_valuator.py -v`
Expected: PASS (new + existing). If an existing test relied on a digest-tier match forcing PENDING, update it to use an instant-tier price (deeply below EV) — that is the intended new behavior.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/valuator/valuator.py tests/apps/test_valuator.py
git commit -m "feat(valuator): force notification PENDING only for instant-tier want matches"
```

---

### Task 4: Notifier want-alert post → instant-only + quiet-hours defer

**Files:**
- Modify: `src/carbuyer/apps/notifier/notifier.py`
- Test: `tests/apps/test_notifier_worker.py`

**Interfaces:**
- Consumes: `delivery_tier` (Task 1), `_in_quiet_hours` (Task 2), `settings.*` (Task 2).
- Produces: `WantAlert` gains `tier: str`; `_load_want_alerts(..., scheduled_end_at, now)` computes it; `_post_want_alerts(..., now)` posts only instant-tier and only outside quiet hours.

- [ ] **Step 1: Write the failing tests**

Add to `tests/apps/test_notifier_worker.py` (use the `_patched_get_session` harness + `_seed_lot` helper):

```python
@pytest.mark.asyncio
async def test_digest_tier_want_match_is_not_posted_instantly(
    _patched_get_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary (digest-tier) want match is left un-notified for the digest."""
    session = _patched_get_session
    _, lot = _seed_lot(session, notification_status="in_progress")
    lot.make, lot.model, lot.year = "Nissan", "Xterra", 2012
    lot.expected_value_cad = Decimal("10000")
    lot.value_mid_cad = Decimal("10000")
    lot.comp_count = 9
    lot.current_high_bid_cad = Decimal("9900")  # ~1% below → digest tier
    want = Search(name="xterra", config={"makes": ["Nissan"], "models": ["Xterra"]})
    session.add(want)
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=lot.id, want_relative_score=0.01)
    session.add(wm)
    await session.flush()
    wm_id, lot_id = wm.id, lot.id

    posted: list[int] = []
    async def fake_post(c, content, lid, *, session=None):  # noqa: ANN001, ANN202
        posted.append(lid)
        return True
    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr("carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242})

    outcome = await _process_one(lot_id, http_session=MagicMock())
    assert posted == []  # not posted instantly
    session.expire_all()
    assert (await session.get(WantMatch, wm_id)).notified_at is None  # left for the digest


@pytest.mark.asyncio
async def test_instant_tier_want_match_deferred_during_quiet_hours(
    _patched_get_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instant-tier match in the quiet window is not posted (rolls into the digest)."""
    from carbuyer.apps.notifier import notifier as nm
    session = _patched_get_session
    _, lot = _seed_lot(session, notification_status="in_progress")
    lot.make, lot.model, lot.year = "Nissan", "Xterra", 2012
    lot.expected_value_cad = Decimal("10000")
    lot.value_mid_cad = Decimal("10000")
    lot.comp_count = 9
    lot.current_high_bid_cad = Decimal("3000")  # deep deal → instant tier
    want = Search(name="xterra", config={"makes": ["Nissan"], "models": ["Xterra"]})
    session.add(want)
    await session.flush()
    session.add(WantMatch(search_id=want.id, lot_id=lot.id, want_relative_score=0.7))
    await session.flush()
    lot_id = lot.id

    monkeypatch.setattr(nm, "_in_quiet_hours", lambda *_: True)
    posted: list[int] = []
    async def fake_post(c, content, lid, *, session=None):  # noqa: ANN001, ANN202
        posted.append(lid)
        return True
    monkeypatch.setattr(notifier_mod, "post_message", fake_post)
    monkeypatch.setattr("carbuyer.apps.notifier.notifier.settings.discord_channels", {"wants": 4242})

    await _process_one(lot_id, http_session=MagicMock())
    assert posted == []  # deferred by quiet hours
```

(`test_process_one_posts_want_match_and_stamps_ledger` already pins that an instant-tier match DOES post outside quiet hours — confirm that test's lot is instant-tier; its `current_high_bid_cad=8000` vs `expected_value_cad=10000` is 20% below → instant, so it stays valid.)

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/apps/test_notifier_worker.py -k "digest_tier_want or deferred_during_quiet" -v`
Expected: FAIL — both currently post (no tiering / quiet-hours yet).

- [ ] **Step 3: Implement**

In `src/carbuyer/apps/notifier/notifier.py`:

Add imports: `from carbuyer.wants.delivery import delivery_tier`.

Add `tier` to `WantAlert`:
```python
@dataclass(frozen=True)
class WantAlert:
    want_match_id: int
    want_name: str
    deal: WantDeal
    tier: str
```

In `_load_want_alerts`, add params `scheduled_end_at: datetime | None` and `now: datetime`, and compute the tier when building each alert (after `deal = score_want_deal(...)`):
```python
        previous_asking = (
            lot.previous_asking_price_cad if isinstance(lot, PrivateListing) else None
        )
        tier = delivery_tier(
            want_relative_score=deal.score,
            offer_price_cad=lot.offer_price,
            previous_asking_price_cad=previous_asking,
            scheduled_end_at=scheduled_end_at,
            now=now,
            deal_threshold=settings.instant_deal_threshold,
            closing_hours=settings.instant_closing_hours,
        )
        alerts.append(WantAlert(want_match_id, want_name, deal, tier))
```

In `_post_want_alerts`, add a `now: datetime` param and gate the loop — at the top of the `for alert in want_alerts:` body, skip non-instant or quiet-hours:
```python
    quiet = _in_quiet_hours(now, settings.quiet_hours_start, settings.quiet_hours_end)
    for alert in want_alerts:
        if alert.tier != "instant" or quiet:
            continue  # digest-tier and quiet-deferred matches wait for the daily digest
        ...
```
(Remove the now-stale "Want alerts bypass quiet hours" sentence from the docstring.)

Update the call sites in `_process_one`. The current code defines `now = datetime.now(UTC)` **after** the read-transaction block (it's the line just below `_load_want_alerts(...)`), but `_load_want_alerts` — which lives inside that block — now needs `now`. So **move** `now = datetime.now(UTC)` to the very top of `_process_one`'s body (before `async with get_session() as s:`), deleting it from its current spot. Then change the existing `_load_want_alerts` call (which already passes `pickup_province`) to also pass `scheduled_end_at` + `now`:
```python
        want_alerts = await _load_want_alerts(
            s, lot,
            pickup_province=(
                auction.pickup_province if auction is not None
                else (lot.location_province if isinstance(lot, PrivateListing) else None)
            ),
            scheduled_end_at=auction.scheduled_end_at if auction is not None else None,
            now=now,
        )
```
Finally, add `now=now` to the `_post_want_alerts(...)` call further down (search for `_post_want_alerts(`; `now` is in scope there). `auction` and the session `s` are already in scope at both sites.

- [ ] **Step 4: Run to verify pass + full notifier file**

Run: `.venv/bin/python -m pytest tests/apps/test_notifier_worker.py -v`
Expected: PASS (new + existing want-match tests).

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/notifier/notifier.py tests/apps/test_notifier_worker.py
git commit -m "feat(notifier): post only instant-tier want matches, defer in quiet hours"
```

---

### Task 5: `render_digest_text`

**Files:**
- Modify: `src/carbuyer/apps/bot/messages.py`
- Test: `tests/apps/test_want_messages.py`

**Interfaces:**
- Produces: `DigestRow` (frozen dataclass: `title: str`, `price_cad: Decimal | None`, `pct_below_market: float | None`, `url: str`) and `render_digest_text(groups: list[tuple[str, list[DigestRow]]]) -> str`.

- [ ] **Step 1: Write the failing test**

Add to `tests/apps/test_want_messages.py`:

```python
def test_render_digest_groups_by_want() -> None:
    from decimal import Decimal
    from carbuyer.apps.bot.messages import DigestRow, render_digest_text
    groups = [
        ("4runner platform", [
            DigestRow(title="2005 Lexus GX 470", price_cad=Decimal("12000"),
                      pct_below_market=0.08, url="http://x/1"),
        ]),
        ("manual xterra", [
            DigestRow(title="2012 Nissan Xterra", price_cad=Decimal("9900"),
                      pct_below_market=0.01, url="http://x/2"),
            DigestRow(title="2010 Nissan Xterra", price_cad=None,
                      pct_below_market=None, url="http://x/3"),
        ]),
    ]
    text = render_digest_text(groups)
    assert "4runner platform" in text
    assert "manual xterra" in text
    assert "GX 470" in text
    assert "http://x/2" in text
    assert "8%" in text  # pct rendered
    assert "None" not in text  # missing price/pct degrade gracefully


def test_render_digest_empty_groups_is_empty_string() -> None:
    from carbuyer.apps.bot.messages import render_digest_text
    assert render_digest_text([]) == ""
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/apps/test_want_messages.py -k digest -v`
Expected: FAIL — `ImportError` for `DigestRow`/`render_digest_text`.

- [ ] **Step 3: Implement** in `src/carbuyer/apps/bot/messages.py`:

```python
@dataclass(slots=True, frozen=True)
class DigestRow:
    title: str
    price_cad: Decimal | None
    pct_below_market: float | None
    url: str


def render_digest_text(groups: list[tuple[str, list["DigestRow"]]]) -> str:
    """One daily digest message: ordinary in-budget want matches grouped by want.
    Empty groups → empty string (the digest job skips posting)."""
    if not groups:
        return ""
    lines = ["\U0001f4f0 Daily want-list digest"]
    for want_name, rows in groups:
        lines.append(f"\n**{want_name}** ({len(rows)})")
        for r in rows:
            price = f"${int(r.price_cad):,}" if r.price_cad is not None else "(no price)"
            pct = f" · {round(r.pct_below_market * 100)}% below market" if r.pct_below_market is not None else ""
            lines.append(f"• {r.title} — {price}{pct}\n  {r.url}")
    return "\n".join(lines)
```

(`dataclass`, `Decimal` are already imported in this module.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/apps/test_want_messages.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/bot/messages.py tests/apps/test_want_messages.py
git commit -m "feat(bot): render_digest_text for the daily want-list digest"
```

---

### Task 6: `apps/digest/` nightly cron job

**Files:**
- Create: `src/carbuyer/apps/digest/__init__.py` (empty), `src/carbuyer/apps/digest/__main__.py`, `src/carbuyer/apps/digest/digest.py`
- Test: `tests/apps/test_digest.py`

**Interfaces:**
- Consumes: `DigestRow`/`render_digest_text` (Task 5); `post_simple_message`, `resolve_channels`, `select_channel`; `WantMatch`/`Search`/`VehicleOffer`.
- Produces: `digest.main(now: datetime | None = None) -> None` + `digest.build_digest(session) -> tuple[list[int], list[tuple[str, list[DigestRow]]]]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/apps/test_digest.py` (mirror `tests/apps/test_distiller.py`'s session usage + `tests/apps/test_notifier_worker.py`'s monkeypatch style):

```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.digest import digest as digest_mod
from carbuyer.db.models import Auction, AuctionLot, PrivateListing, Search, WantMatch


async def _seed_unnotified_match(session: AsyncSession, *, enabled: bool = True) -> int:
    listing = PrivateListing(
        source="kijiji", source_listing_id="K1", url="http://k/1",
        title="2010 Nissan Xterra", make="Nissan", model="Xterra", year=2010,
        asking_price_cad=Decimal("9900"), seller_type="private",
        location_province="AB", listing_status="active",
        expected_value_cad=Decimal("10000"), value_mid_cad=Decimal("10000"), comp_count=9,
    )
    want = Search(name="xterra", config={}, enabled=enabled)
    session.add_all([listing, want])
    await session.flush()
    wm = WantMatch(search_id=want.id, lot_id=listing.id, want_relative_score=0.01)
    session.add(wm)
    await session.flush()
    return wm.id


@pytest.mark.asyncio
async def test_build_digest_groups_unnotified_enabled_matches(session: AsyncSession) -> None:
    await _seed_unnotified_match(session)
    await _seed_unnotified_match(session, enabled=False)  # muted want excluded
    await session.flush()
    match_ids, groups = await digest_mod.build_digest(session)
    assert len(match_ids) == 1
    assert groups and groups[0][0] == "xterra"
    assert groups[0][1][0].title.endswith("Nissan Xterra")


@pytest.mark.asyncio
async def test_build_digest_empty_when_nothing_unnotified(session: AsyncSession) -> None:
    match_ids, groups = await digest_mod.build_digest(session)
    assert match_ids == [] and groups == []
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/apps/test_digest.py -v`
Expected: FAIL — `ModuleNotFoundError: carbuyer.apps.digest`.

- [ ] **Step 3: Implement**

Create `src/carbuyer/apps/digest/__init__.py` (empty file).

Create `src/carbuyer/apps/digest/__main__.py`:
```python
from carbuyer.apps._runner import run_worker
from carbuyer.apps.digest.digest import main

if __name__ == "__main__":
    run_worker("digest", main)
```

Create `src/carbuyer/apps/digest/digest.py`:
```python
"""Daily want-list digest — nightly cron worker.

Distiller-shaped: no LISTEN, no claim, single-instance, runs once and exits.
Delivers every want match still un-notified (digest-tier matches plus instant
matches deferred by quiet hours) as one grouped Discord message, then stamps
notified_at so each is delivered exactly once. Schedule at quiet_hours_end.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import aiohttp
from sqlalchemy import select, update

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import DigestRow, render_digest_text
from carbuyer.apps.notifier.channel_resolver import resolve_channels
from carbuyer.apps.notifier.discord_post import post_simple_message
from carbuyer.db.models import Search, VehicleOffer, WantMatch
from carbuyer.db.session import get_session
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("digest")


def _vehicle_title(o: VehicleOffer) -> str:
    parts = [str(o.year or ""), o.make or "", o.model or "", o.trim or ""]
    return " ".join(p for p in parts if p).strip() or (o.title or f"Offer #{o.id}")


async def build_digest(
    session,  # noqa: ANN001 -- AsyncSession; untyped to keep the import surface small
) -> tuple[list[int], list[tuple[str, list[DigestRow]]]]:
    """Un-notified, non-dismissed matches for enabled wants, grouped by want name.
    Returns (match_ids, groups) — match_ids to stamp, groups to render."""
    rows = (
        await session.execute(
            select(WantMatch.id, WantMatch.want_relative_score, Search.name, VehicleOffer)
            .join(Search, Search.id == WantMatch.search_id)
            .join(VehicleOffer, VehicleOffer.id == WantMatch.lot_id)
            .where(
                WantMatch.notified_at.is_(None),
                WantMatch.dismissed.is_(False),
                Search.enabled.is_(True),
            )
            .order_by(Search.name, WantMatch.want_relative_score.desc().nulls_last())
        )
    ).all()
    match_ids: list[int] = []
    grouped: dict[str, list[DigestRow]] = {}
    for match_id, score, want_name, offer in rows:
        match_ids.append(match_id)
        grouped.setdefault(want_name, []).append(
            DigestRow(
                title=_vehicle_title(offer),
                price_cad=offer.offer_price,
                pct_below_market=score,
                url=offer.url,
            )
        )
    return match_ids, list(grouped.items())


async def main(now: datetime | None = None) -> None:
    if now is None:
        now = datetime.now(UTC)
    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN not configured")
        return
    settings.discord_channels = cast(
        "dict[str, int | str]",
        await resolve_channels(
            settings.discord_channels,
            guild_id=settings.discord_guild_id,
            bot_token=settings.discord_bot_token,
        ),
    )
    channel_key = select_channel(trigger="want_match", score=None)  # "wants"
    channel_id = settings.discord_channels.get(channel_key)
    if not isinstance(channel_id, int):
        log.warning("no wants channel configured for digest", channel_key=channel_key)
        return

    async with get_session() as session:
        match_ids, groups = await build_digest(session)
    if not match_ids:
        log.info("digest: nothing to deliver")
        return

    content = render_digest_text(groups)
    async with aiohttp.ClientSession() as http:
        posted = await post_simple_message(channel_id, content, session=http)
    if not posted:
        log.warning("digest post failed; leaving matches un-notified for next run")
        return

    async with get_session() as session, session.begin():
        await session.execute(
            update(WantMatch).where(WantMatch.id.in_(match_ids)).values(notified_at=now)
        )
    log.info("digest delivered", matches=len(match_ids), wants=len(groups))
```

- [ ] **Step 4: Run to verify pass + full suite**

Run: `.venv/bin/python -m pytest tests/apps/test_digest.py -v && .venv/bin/python -m pytest -q`
Expected: PASS; full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/digest/ tests/apps/test_digest.py
git commit -m "feat(digest): nightly want-list digest cron worker"
```

---

## Verification (after all tasks)

- [ ] Full suite green: `.venv/bin/python -m pytest -q`
- [ ] Types: `.venv/bin/pyright src/carbuyer/wants/ src/carbuyer/apps/notifier/ src/carbuyer/apps/valuator/ src/carbuyer/apps/digest/ src/carbuyer/apps/bot/messages.py`
- [ ] Lint: `.venv/bin/ruff check src/carbuyer/ tests/` (the pre-existing `RUF003` ambiguous-`×` in `openai_provider.py:239` is not ours)
- [ ] README/run docs: note the `digest` cron (schedule at `QUIET_HOURS_END`) next to the distiller's cron entry.
- [ ] `reuse-reviewer` agent on the diff before merge.
