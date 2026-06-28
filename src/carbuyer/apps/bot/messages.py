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
    top_red_flags: tuple[str, ...]
    top_green_flags: tuple[str, ...]
    suspicious_underprice: bool
    scheduled_end_at: datetime | None
    # Private-listing price-drop re-alert: the asking price before the latest
    # drop (None for auctions / no prior drop).
    previous_asking_cad: Decimal | None = None
    # NHTSA reliability signal (None = not looked up).
    recall_count: int | None = None
    complaint_count: int | None = None


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
        f"Rarity: {rarity}\n"
        f"{d.url}"
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
        f"{('✅ ' + flags) if flags else ''}\n"
        f"{d.url}"
    ).rstrip()


def render_closing_soon_text(d: LotEmbedData) -> str:
    title = _vehicle_title(d)
    bid = f"${int(d.current_high_bid_cad):,}" if d.current_high_bid_cad else "(no bid yet)"
    all_in = f"${int(d.all_in_cad):,}" if d.all_in_cad else "?"
    if d.value_low_cad and d.value_high_cad:
        rng = f"${int(d.value_low_cad):,}–${int(d.value_high_cad):,}"  # noqa: RUF001
    else:
        rng = "(uncomped)"
    return (
        f"⏰ Closes in 1h — {title} ({d.location})\n"
        f"Current bid: {bid}  →  All-in: {all_in}\n"
        f"Estimated value: {rng}\n"
        f"{d.url}"
    )


def render_lot_extended_text(d: LotEmbedData) -> str:
    title = _vehicle_title(d)
    end = d.scheduled_end_at.strftime("%b %d %H:%M UTC") if d.scheduled_end_at else "?"
    bid = f"${int(d.current_high_bid_cad):,}" if d.current_high_bid_cad else "(no bid)"
    return (
        f"🔁 Soft-close extension — {title} ({d.location})\n"
        f"New end time: {end}\n"
        f"Current bid: {bid} (bid landed in final minutes — auction extended)\n"
        f"{d.url}"
    )


def render_want_match_text(
    d: LotEmbedData,
    *,
    want_name: str,
    pct_below_market: float | None,
    dollars_below_market_cad: Decimal | None,
    dollars_under_ceiling_cad: Decimal | None,
    comp_count: int | None,
) -> str:
    """Alert for a lot that matched a user's want. Shows the want-relative deal
    (% and $ vs market, $ under the want's ceiling, comp count) and degrades
    gracefully to '(not enough comps to price)' when the lot is uncomped."""
    title = _vehicle_title(d)
    price = f"${int(d.current_high_bid_cad):,}" if d.current_high_bid_cad else "(no price yet)"
    drop_line = ""
    if (
        d.previous_asking_cad is not None
        and d.current_high_bid_cad is not None
        and d.previous_asking_cad > d.current_high_bid_cad
    ):
        drop = int(d.previous_asking_cad - d.current_high_bid_cad)
        drop_line = (
            f"\U0001f4c9 Price drop: ${int(d.previous_asking_cad):,} -> "
            f"${int(d.current_high_bid_cad):,} (down ${drop:,})\n"
        )
    parts: list[str] = []
    if pct_below_market is not None and dollars_below_market_cad is not None:
        pct = round(pct_below_market * 100)
        amt = int(dollars_below_market_cad)
        if amt >= 0:
            parts.append(f"{pct}% (${amt:,}) below market")
        else:
            parts.append(f"{-pct}% (${-amt:,}) above market")
    if comp_count is not None:
        parts.append(f"{comp_count} comps")
    deal_line = " · ".join(parts) if parts else "(not enough comps to price)"
    budget = ""
    if dollars_under_ceiling_cad is not None:
        budget = f"\n${int(dollars_under_ceiling_cad):,} under your budget"
    reliability = ""
    if d.recall_count is not None or d.complaint_count is not None:
        recalls = d.recall_count if d.recall_count is not None else "?"
        complaints = d.complaint_count if d.complaint_count is not None else "?"
        reliability = f"\n\U0001f527 NHTSA: {recalls} recalls · {complaints} complaints"
    return (
        f"\U0001f3af Matches your want “{want_name}”\n"
        f"{drop_line}"
        f"{title} ({d.location})\n"
        f"Price: {price} · {deal_line}{budget}{reliability}\n"
        f"{d.url}"
    )


def render_needs_plugin_text(
    *,
    auction_id: int,
    url: str,
    auctioneer_name: str | None,
    pickup_city: str | None,
    pickup_province: str | None,
    scheduled_start_at: datetime | None,
) -> str:
    location = ", ".join(filter(None, [pickup_city, pickup_province])) or "?"
    when = scheduled_start_at.strftime("%b %d") if scheduled_start_at else "(start date unknown)"
    return (
        f"🔌 NEW PLATFORM — needs a scraper plugin\n"
        f"Auctioneer: {auctioneer_name or '(unknown)'}\n"
        f"Location: {location}\n"
        f"Auction starts: {when}\n"
        f"URL: {url}\n\n"
        f"Add a plugin under src/carbuyer/sources/<name>/ before the auction closes "
        f"to capture lot data. After deploying the plugin, click 'Retry routing' "
        f"on /needs-plugin (auction id {auction_id}) to reprocess this auction."
    )
