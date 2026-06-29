"""Map a notification trigger to its destination Discord channel.

Phase 5 splits notifications across topical channels so high-signal hits don't
drown in routine chatter. The routing table is intentionally small and
deterministic — the notifier worker calls this once per trigger; everything
else (rate limiting, idempotency, embed building) lives elsewhere.
"""
from __future__ import annotations

from typing import Final, Literal

ChannelKey = Literal[
    "wants",
    "auction_closing",
    "auction_watch",
    "vision_updates",
    "system_health",
    "needs_plugin",
]


# Triggers that map 1:1 to a channel.
_FIXED_ROUTES: Final[dict[str, ChannelKey]] = {
    "want_match": "wants",
    "closing_soon": "auction_closing",
    "lot_extended": "auction_closing",
    "vision_update": "vision_updates",
    "system": "system_health",
    "needs_plugin": "needs_plugin",
}


def select_channel(*, trigger: str, score: float | None) -> ChannelKey:
    return _FIXED_ROUTES.get(trigger, "auction_watch")
