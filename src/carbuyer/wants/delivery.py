"""Delivery tier for a want-match: instant ping vs the daily digest.

A pure classifier (inputs injected — no DB, no settings), shared by the valuator's
force-PENDING gate (which classifies from the stored want_relative_score at
valuation time) and the notifier's instant post (which re-classifies from the
freshest offer price, so a since-risen auction bid correctly downgrades an instant
match to digest). The nightly digest delivers every still-un-notified match
regardless of tier, so a downgrade never loses a match — it only defers it to the
morning. A match is instant when it is a standout deal, an uncomped wanted vehicle,
a price-drop on an already-matched listing, or an auction lot closing soon.
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
    # An uncomped match (score=None — we couldn't price the deal) is surfaced
    # instantly rather than buried in the digest: WG4 holds that a wanted vehicle
    # alerts even when unpriceable, and these are low-volume (wanted models only).
    if want_relative_score is None or want_relative_score >= deal_threshold:
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
