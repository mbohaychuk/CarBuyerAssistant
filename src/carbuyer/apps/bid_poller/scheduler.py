"""Tiered-cadence scheduler for bid polling.

Computes how long to wait before re-polling a given lot based on its distance
from closing. The closer to end time, the shorter the delay — down to 30-second
granularity in the final 10 minutes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from carbuyer.db.enums import LotStatus


def next_poll_delay(*, scheduled_end: datetime | None, now: datetime, status: str) -> timedelta:  # noqa: PLR0911
    """How long until we should next poll this lot.

    Cadence (open lots, ordered by time-to-close):
      ≤ 10 min    → 30s
      ≤ 1 hr      → 1 min
      ≤ 2 hr      → 5 min
      ≤ 24 hr     → 15 min
      > 24 hr     → 60 min
    Closed/unsold/sold → 24 hr (very rare re-check).
    No scheduled_end → 60 min.
    """
    if scheduled_end is None:
        return timedelta(minutes=60)
    if status in {LotStatus.CLOSED, LotStatus.UNSOLD, LotStatus.SOLD}:
        return timedelta(hours=24)  # very rare re-check
    delta = scheduled_end - now
    if delta <= timedelta(minutes=10):
        return timedelta(seconds=30)
    if delta <= timedelta(hours=1):
        return timedelta(minutes=1)
    if delta <= timedelta(hours=2):
        return timedelta(minutes=5)
    if delta <= timedelta(hours=24):
        return timedelta(minutes=15)
    return timedelta(minutes=60)
