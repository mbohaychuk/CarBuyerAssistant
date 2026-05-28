"""Map a notification trigger to its destination Discord channel.

Phase 5 splits notifications across seven topical channels so high-signal
hits ("hot deals") don't drown in routine watchlist chatter. The routing
table is intentionally small and deterministic — Phase 6's notifier worker
calls this once per trigger; everything else (rate limiting, idempotency,
embed building) lives elsewhere.

The 0.20 deal-score cutoff (``HOT_DEAL_SCORE_THRESHOLD``) splits
notify-eligible ``going_cheap`` deals into ``hot_deals`` (>= threshold) and
``watchlist`` (< threshold). This routing is independent of *whether* a
``going_cheap`` notification fires at all — that gate is the time-to-close
tier table (``GOING_CHEAP_TIERS`` / ``cheap_threshold`` in
``notifier/triggers.py``), not ``settings.notify_threshold``.
"""
from __future__ import annotations

from typing import Final, Literal

ChannelKey = Literal[
    "early_warning",
    "hot_deals",
    "watchlist",
    "auction_closing",
    "auction_watch",
    "vision_updates",
    "system_health",
    "needs_plugin",
]

HOT_DEAL_SCORE_THRESHOLD: Final[float] = 0.20


# Triggers that always map 1:1 to a channel regardless of score.
_FIXED_ROUTES: Final[dict[str, ChannelKey]] = {
    "early_warning": "early_warning",
    "closing_soon": "auction_closing",
    "bid_trajectory": "auction_closing",
    "lot_extended": "auction_closing",
    "vision_update": "vision_updates",
    "system": "system_health",
    "needs_plugin": "needs_plugin",
}


def select_channel(*, trigger: str, score: float | None) -> ChannelKey:
    fixed = _FIXED_ROUTES.get(trigger)
    if fixed is not None:
        return fixed
    if trigger == "going_cheap":
        if score is not None and score >= HOT_DEAL_SCORE_THRESHOLD:
            return "hot_deals"
        return "watchlist"
    return "watchlist"
