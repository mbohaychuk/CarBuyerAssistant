"""Buyer-leverage signals for a private listing (design-doc §5d).

Days-on-market + a compact price-drop summary — how long a listing has sat and
how far its price has fallen — as negotiating context (not urgency). Pure helpers
over the listing's own fields; the same one-line string is shown on the Discord
want-match alert and the dashboard want-detail page.
"""
from __future__ import annotations

from datetime import datetime

from carbuyer.db.models import PrivateListing, VehicleOffer


def effective_days_on_market(
    days_on_market: int | None, first_seen_at: datetime | None, now: datetime,
) -> int | None:
    """Prefer the source value (the seller's true listing age); fall back to how
    long we've seen it (now - first_seen_at); None when we have neither."""
    if days_on_market is not None:
        return days_on_market
    if first_seen_at is not None:
        return max(0, (now - first_seen_at).days)
    return None


def buyer_leverage_line(offer: VehicleOffer, now: datetime) -> str | None:
    """A compact buyer-leverage line for a PRIVATE listing, or None (auctions /
    no data): e.g. "listed 90 days · down $3,000 (17%) from $18,000 · 2 drops"."""
    if not isinstance(offer, PrivateListing):
        return None

    clauses: list[str] = []
    dom = effective_days_on_market(offer.days_on_market, offer.first_seen_at, now)
    if dom is not None:
        clauses.append(f"listed {dom} days")

    orig = offer.original_asking_price_cad
    cur = offer.asking_price_cad
    # `orig > 0` guards the pct division and rejects a non-positive baseline
    # (a zero/negative original price is not meaningful leverage).
    if orig is not None and cur is not None and orig > cur and orig > 0:
        drop = orig - cur
        pct = round(drop / orig * 100)
        drop_clause = f"down ${int(drop):,} ({pct}%) from ${int(orig):,}"
        n = offer.price_drop_count
        if n >= 1:
            drop_clause += f" · {n} drop{'s' if n != 1 else ''}"
        clauses.append(drop_clause)

    return " · ".join(clauses) if clauses else None
