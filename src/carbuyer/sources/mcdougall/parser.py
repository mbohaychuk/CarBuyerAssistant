"""McDougall HTML parsers.

McDougall is server-rendered HTML; no GraphQL or API. Two response shapes
matter for ingestion:

  1. Vehicles catalog page (products.php?category=Vehicles[&p=N]) — a
     cross-auction list of all vehicle lots, with one summary card per lot.
     The lot's parent auction GUID is NOT exposed here; commit #6 adds the
     detail-page parser that surfaces it.
  2. Lot detail page (products-full-view.php?arg=<GUID>) — handled in
     commit #6.

Both pages use the same div.auction-product-item card structure for lot
summaries on the catalog page; selectors here target that block.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from selectolax.parser import HTMLParser, Node

from carbuyer.shared.logging import get_logger

_log = get_logger("sources.mcdougall.parser")

# 8-4-4-4-12 hex; matches the GUIDs McDougall uses for both lot ids and
# auction-event ids. Captured inside ?arg=... in detail-page URLs.
_GUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
)

# "$5,400.00" -> Decimal("5400.00"). Strips $, commas, CAD suffix.
_BID_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")

# "Lot: 4Status: Open" — no whitespace between "4" and "Status" because the
# <span class="status"> sits flush against the number. Capture both halves.
_LOT_STATUS_RE = re.compile(
    r"Lot:\s*(?P<lot>\S+?)Status:\s*(?P<status>\S+)",
)


@dataclass(slots=True, frozen=True)
class CatalogEntry:
    """One lot card off the cross-auction Vehicles catalog page.

    Parent-auction GUID is intentionally not here — it's only available on
    the per-lot detail page (commit #6 surfaces it).
    """

    lot_guid: str                          # ?arg=<GUID> from the lot URL
    lot_url: str                           # absolute, canonical
    title: str                             # raw "1987 Chevrolet Monte Carlo ..."
    location: str | None                   # "<addr>, <city>, <prov>" raw
    lot_number: str | None                 # "4"
    status_raw: str | None                 # "Open" | "Closed" | ...
    current_high_bid_cad: Decimal | None   # None when no bids yet
    scheduled_end_at: datetime | None      # UTC; parsed from hidden input
    photo_url: str | None                  # thumbnail src


def _text_of(node: Node | None) -> str:
    """Empty string for missing nodes — keeps callers branch-free."""
    return node.text(strip=True) if node is not None else ""


def _absolutize(href: str) -> str:
    """McDougall mixes absolute and relative hrefs on the same page; normalize
    everything to absolute, lowercase host. canonicalize_url further strips
    tracking params at the source-layer boundary."""
    if href.startswith("http"):
        return href
    return f"https://www.mcdougallauction.com/{href.lstrip('/')}"


def _parse_bid(text: str) -> Decimal | None:
    m = _BID_RE.search(text)
    if m is None:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _parse_end_time(card: Node) -> datetime | None:
    """McDougall emits an ISO 8601 UTC value in a hidden input — that's the
    authoritative end-time. The visible "close-date-timezone" div is JS-
    formatted from this value and may be empty pre-render."""
    node = card.css_first("input[id^='txtLotEndDate']")
    if node is None:
        return None
    raw = (node.attributes.get("value") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_one_card(card: Node) -> CatalogEntry | None:
    """Parse one .auction-product-item block. Returns None on malformed
    cards rather than raising — one bad card must not abort the page."""
    href_node = card.css_first("a[href*='products-full-view.php']")
    if href_node is None:
        return None
    href = href_node.attributes.get("href") or ""
    if not href:
        return None
    guid_match = _GUID_RE.search(href)
    if guid_match is None:
        return None
    lot_guid = guid_match.group(0)

    title_node = card.css_first("div.item-title h4 a") or href_node
    title = _text_of(title_node)

    location_node = card.css_first("div.item-location p")
    location_raw = _text_of(location_node)
    location = (
        location_raw.removeprefix("Location:").strip() or None
        if location_raw else None
    )

    lot_status_node = card.css_first("div.lot-status p")
    lot_status_text = _text_of(lot_status_node)
    lot_status_match = _LOT_STATUS_RE.search(lot_status_text)
    lot_number = lot_status_match.group("lot") if lot_status_match else None
    status_raw = lot_status_match.group("status") if lot_status_match else None

    current_bid_node = card.css_first("div.current-bid p")
    current_bid = _parse_bid(_text_of(current_bid_node)) if current_bid_node else None

    img_node = card.css_first("div.item-img img")
    photo_url = img_node.attributes.get("src") if img_node else None

    return CatalogEntry(
        lot_guid=lot_guid,
        lot_url=_absolutize(href),
        title=title,
        location=location,
        lot_number=lot_number,
        status_raw=status_raw,
        current_high_bid_cad=current_bid,
        scheduled_end_at=_parse_end_time(card),
        photo_url=photo_url,
    )


def parse_catalog_page(html: str) -> list[CatalogEntry]:
    """Walk all .auction-product-item cards on a Vehicles catalog page.

    Skips (with a log line) any card whose lot GUID can't be extracted —
    that's a structural defect, not a recoverable parse error. Returns an
    empty list when no cards are present (signals "no more pages" to the
    pagination walker).
    """
    tree = HTMLParser(html)
    entries: list[CatalogEntry] = []
    for card in tree.css("div.auction-product-item"):
        parsed = _parse_one_card(card)
        if parsed is None:
            _log.warning("skipping malformed catalog card")
            continue
        entries.append(parsed)
    return entries
