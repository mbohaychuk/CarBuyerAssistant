"""McDougall HTML parsers.

McDougall is server-rendered HTML; no GraphQL or API. Two response shapes
matter for ingestion:

  1. Vehicles catalog page (products.php?category=Vehicles[&p=N]) — a
     cross-auction list of all vehicle lots, with one summary card per lot.
     The lot's parent auction GUID is NOT exposed here.
  2. Lot detail page (products-full-view.php?arg=<GUID>) — full vehicle
     details (mileage / VIN / photos / engine / etc), plus the parent
     auction GUID embedded in an outbound link, plus the buyer premium
     terms text. Bid + status + close time are also here for poll_bid.

Both pages use the same div.auction-product-item card structure for lot
summaries on the catalog page; selectors here target that block.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

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

# "47,800km" -> 47800. Allows comma thousands sep + optional decimal point.
_MILEAGE_RE = re.compile(r"([\d,]+)(?:\.\d+)?\s*km", re.IGNORECASE)

# "Daspiper - 31 bids" -> 31. The bidder name is behind login so we skip it.
_BID_COUNT_RE = re.compile(r"(\d+)\s*bids?\b", re.IGNORECASE)

# "15% Buyers Premium to a Max of $2000 per lot and a Minimum of $20 per lot".
# Captures percent + max + min as integers. DOTALL so the terms paragraph
# survives arbitrary whitespace / linebreaks between Max and Min in source HTML.
_BUYER_PREMIUM_RE = re.compile(
    r"(\d+)\s*%\s*Buyers? Premium"
    r".*?Max(?:imum)?\s*(?:of)?\s*\$\s*(\d+)"
    r".*?Min(?:imum)?\s*(?:of)?\s*\$\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
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


# ── Lot detail page (products-full-view.php?arg=<GUID>) ──────────────────────


@dataclass(slots=True, frozen=True)
class LotDetail:
    """Parsed shape of one McDougall lot detail page.

    Fields that map directly to ``RawLot`` are exposed by name. Free-form
    secondaries (engine, transmission, door count, etc) live in ``extras``
    for downstream enrichment; promote any to a real field once 2+ sources
    surface the equivalent.

    Auction-level fields here (auction_guid, buyer_premium_*, pickup_location)
    are everything we need to build a minimum-viable RawAuction without a
    second HTTP round to auction-event.php. Title of the parent auction is
    not on this page; left None until/unless we add an auction-event fetcher.
    """

    lot_guid: str
    auction_guid: str | None
    title: str | None                       # full product title (page <h1>)
    description: str | None                 # consignor remarks block
    vin: str | None
    mileage_km: int | None
    current_high_bid_cad: Decimal | None
    bid_count_visible: int | None
    scheduled_end_at: datetime | None
    pickup_location: str | None
    photos: list[str]
    buyer_premium_pct: Decimal | None
    buyer_premium_max_cad: Decimal | None
    buyer_premium_min_cad: Decimal | None
    extras: dict[str, Any] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=dict,
    )


# Auction-event URL anywhere on the lot-detail page → ?arg=<GUID>.
_AUCTION_EVENT_LINK_RE = re.compile(rf"auction-event\.php\?arg=({_GUID_RE.pattern})")


def _text_after_label(tree: HTMLParser, label_text: str) -> str | None:
    """Find a <span>label</span> and return the trailing text of its parent
    element. Tolerates leading whitespace and stops at first child element.

    McDougall renders structured fields as ``<tr><td><span>Engine*:</span>
    5.7L V8 Gas</td></tr>`` — the leaf value isn't reachable by CSS selector
    alone. We pull the `<span>` and read its parent's full text minus the
    label.
    """
    target = label_text.casefold()
    for span in tree.css("span"):
        if span.text(strip=True).casefold() != target:
            continue
        parent = span.parent
        if parent is None:
            continue
        full = parent.text(strip=True)
        # full looks like "Engine*: 5.7L V8 Gas" (label was within parent text).
        idx = full.casefold().find(target)
        if idx == -1:
            continue
        value = full[idx + len(label_text):].strip()
        return value or None
    return None


def _parse_mileage_km(value: str | None) -> int | None:
    if value is None:
        return None
    m = _MILEAGE_RE.search(value)
    if m is None:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_vin(tree: HTMLParser) -> str | None:
    # VIN is "<p><strong>Serial Number:</strong> <VIN></p>".
    for strong in tree.css("strong"):
        if strong.text(strip=True).casefold().startswith("serial number"):
            parent = strong.parent
            if parent is None:
                continue
            full = parent.text(strip=True)
            # "Serial Number: 1G1GZ11H7HP127810" → strip prefix.
            colon = full.find(":")
            if colon == -1:
                return None
            return full[colon + 1:].strip() or None
    return None


def _parse_description(tree: HTMLParser) -> str | None:
    # Description is the <td> body following a "<span>Consignor Remarks:</span>".
    for span in tree.css("span"):
        if "consignor remarks" not in span.text(strip=True).casefold():
            continue
        parent = span.parent
        if parent is None:
            continue
        # The label and the prose live in the same <td>; selectolax returns
        # the prose nested under <p> elements in HTML order. Stitch them all.
        paragraphs = [p.text(strip=True) for p in parent.css("p") if p.text(strip=True)]
        joined = "\n".join(paragraphs).strip()
        return joined or None
    return None


def _parse_photos(tree: HTMLParser) -> list[str]:
    # Full-size photos live in `.product-gallery a[data-fancybox="gallery"]`
    # href. Skip <a class="video"> (Youtube embeds — not vehicle photos).
    photos: list[str] = []
    for anchor in tree.css("div.product-gallery a[data-fancybox='gallery']"):
        if "video" in (anchor.attributes.get("class") or ""):
            continue
        href = anchor.attributes.get("href") or ""
        if href:
            photos.append(href)
    return photos


def _parse_current_bid(tree: HTMLParser) -> Decimal | None:
    node = tree.css_first("span#spnBidPrice")
    if node is None:
        return None
    text = node.text(strip=True).replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_bid_count(tree: HTMLParser) -> int | None:
    node = tree.css_first("p.current-bidder")
    if node is None:
        return None
    m = _BID_COUNT_RE.search(node.text(strip=True))
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _parse_end_time_root(tree: HTMLParser) -> datetime | None:
    # Detail pages carry three matching inputs: a bare ``id=txtLotEndDate``
    # (sometimes empty, used by JS), a bare populated one, and a GUID-
    # suffixed one. Pick the first non-empty value rather than the first
    # match — the bare empty input would otherwise short-circuit us.
    for node in tree.css("input[id^='txtLotEndDate']"):
        raw = (node.attributes.get("value") or "").strip()
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _parse_auction_guid(html: str) -> str | None:
    m = _AUCTION_EVENT_LINK_RE.search(html)
    return m.group(1) if m else None


def _parse_buyer_premium(html: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Returns (pct, max_cad, min_cad). Parses from the terms paragraph;
    falls back to (None, None, None) if the paragraph is absent or mangled.

    Failing closed (rather than defaulting to 15%/2000/20) means a terms
    change surfaces in the dashboard as missing values, not silently wrong
    all-in pricing.
    """
    m = _BUYER_PREMIUM_RE.search(html)
    if m is None:
        return (None, None, None)
    try:
        pct = Decimal(m.group(1)) / Decimal("100")
        max_cad = Decimal(m.group(2))
        min_cad = Decimal(m.group(3))
    except InvalidOperation:
        return (None, None, None)
    return (pct, max_cad, min_cad)


def parse_lot_detail(html: str, *, lot_guid: str) -> LotDetail:
    """Parse one products-full-view.php?arg=<GUID> page into a LotDetail.

    ``lot_guid`` is passed in by the caller (it knows the LotRef) rather
    than re-extracted from the URL — keeps the parser independent of the
    page URL it came from.
    """
    tree = HTMLParser(html)

    title_node = tree.css_first("div.full-product-title h1")
    title = title_node.text(strip=True) if title_node is not None else None

    # "Showing Unverified" sits in the label, not the value. Track which
    # label we matched so the flag survives into extras.
    mileage_unverified = True
    mileage_raw = _text_after_label(tree, "Mileage (Showing Unverified)*:")
    if mileage_raw is None:
        mileage_unverified = False
        mileage_raw = _text_after_label(tree, "Mileage*:")
    mileage_km = _parse_mileage_km(mileage_raw)

    pickup_location = _text_after_label(tree, "Pick up location:")

    pct, max_cad, min_cad = _parse_buyer_premium(html)

    extras: dict[str, Any] = {}
    for label, key in (
        ("Engine*:", "engine"),
        ("Transmission Type*:", "transmission"),
        ("Door Count:", "door_count"),
        ("Seating Capacity:", "seating_capacity"),
    ):
        value = _text_after_label(tree, label)
        if value is not None:
            extras[key] = value
    if mileage_km is not None and mileage_unverified:
        extras["mileage_unverified"] = True

    return LotDetail(
        lot_guid=lot_guid,
        auction_guid=_parse_auction_guid(html),
        title=title,
        description=_parse_description(tree),
        vin=_parse_vin(tree),
        mileage_km=mileage_km,
        current_high_bid_cad=_parse_current_bid(tree),
        bid_count_visible=_parse_bid_count(tree),
        scheduled_end_at=_parse_end_time_root(tree),
        pickup_location=pickup_location,
        photos=_parse_photos(tree),
        buyer_premium_pct=pct,
        buyer_premium_max_cad=max_cad,
        buyer_premium_min_cad=min_cad,
        extras=extras,
    )
