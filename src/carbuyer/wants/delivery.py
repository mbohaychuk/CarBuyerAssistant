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
