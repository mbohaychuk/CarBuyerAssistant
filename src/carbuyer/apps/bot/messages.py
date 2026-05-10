"""Plaintext renderers for notifier messages.

These produce the fallback text Discord shows when a rich embed cannot
render (mobile push previews, accessibility tools, log mirroring). The
renderers take a frozen ``LotEmbedData`` snapshot built by the notifier
worker (Phase 6) so the rendering is pure and trivially testable —
no DB session, no settings, no clock.

The string content uses the same Unicode glyphs the embed itself will
use (en dash, middle dot, decorative emoji) for visual consistency.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(slots=True, frozen=True)
class LotEmbedData:
    lot_id: int
    url: str
    title: str
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    location: str
    current_high_bid_cad: Decimal | None
    all_in_cad: Decimal | None
    expected_value_cad: Decimal | None
    value_low_cad: Decimal | None
    value_high_cad: Decimal | None
    price_deal_score: float | None
    rarity_score: float | None
    confidence_bucket: str | None
    condition_categorical: str | None
    top_red_flags: list[str]
    top_green_flags: list[str]
    suspicious_underprice: bool
    scheduled_end_at: datetime | None


def _vehicle_title(d: LotEmbedData) -> str:
    parts = [str(d.year or ""), d.make or "", d.model or "", d.trim or ""]
    return " ".join(p for p in parts if p).strip()


def render_early_warning_text(d: LotEmbedData) -> str:
    title = _vehicle_title(d)
    end = d.scheduled_end_at.strftime("%b %d") if d.scheduled_end_at else "?"
    bid = f"${int(d.current_high_bid_cad):,}" if d.current_high_bid_cad else "(no bid yet)"
    if d.value_low_cad and d.value_high_cad:
        rng = f"${int(d.value_low_cad):,}–${int(d.value_high_cad):,}"  # noqa: RUF001 (en dash for Discord display)
    else:
        rng = "(uncomped)"
    rarity = ", ".join(d.top_green_flags[:3]) or "rare/desirable"
    return (
        f"⭐ RARE FIND — {title} ({d.location})\n"
        f"Closes {end}\n"
        f"Current bid: {bid} · Estimated value: {rng}\n"
        f"Rarity: {rarity}"
    )


def render_going_cheap_text(d: LotEmbedData) -> str:
    title = _vehicle_title(d)
    bid = f"${int(d.current_high_bid_cad):,}" if d.current_high_bid_cad else "no bid"
    all_in = f"${int(d.all_in_cad):,}" if d.all_in_cad else "?"
    ev = f"${int(d.expected_value_cad):,}" if d.expected_value_cad else "?"
    margin = ""
    if d.expected_value_cad and d.all_in_cad:
        m = int(d.expected_value_cad - d.all_in_cad)
        margin = f"  ·  Margin at current bid: ${m:,}"
    flags = ", ".join(d.top_green_flags[:3])
    prefix = "⚠ PRICED BELOW TYPICAL LOW END\n" if d.suspicious_underprice else ""
    return (
        f"{prefix}\U0001f4b0 Going cheap — {title}\n"
        f"{d.location}\n"
        f"Current bid: {bid}  →  All-in: {all_in}\n"
        f"Estimated value: {ev}{margin}\n"
        f"Confidence: {d.confidence_bucket or '?'} · Condition: {d.condition_categorical or '?'}\n"
        f"{('✅ ' + flags) if flags else ''}"
    ).rstrip()
