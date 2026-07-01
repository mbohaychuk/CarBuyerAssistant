"""Turn a Kijiji 'Cars & Trucks' search page into ``RawListing``s.

Kijiji is a Next.js app that embeds a schema.org ``ItemList`` of ``Car`` objects
as ``<script type="application/ld+json">`` — a stable SEO contract that is
richer and steadier than the rendered DOM or the normalized Apollo cache. We
read that one block. Fields absent from it degrade gracefully:

* ``seller_type`` comes from the listing image path (``ca-prod-fsbo-ads`` =
  private sale, ``ca-prod-dealer-ads`` = dealer).
* ``days_on_market`` is left ``None`` — the buyer-leverage code computes it from
  our own ``first_seen_at`` (precise), not Kijiji's fuzzy "1 wk ago" text.
* ``location_province`` is left ``None``; the city slug from the URL goes in
  ``extra``.

Legal (carried from research): the image URL is stored for deep-linking only —
never the photo bytes (Trader v. CarGurus) — and no seller PII (PIPEDA); the
ld+json carries none.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from selectolax.parser import HTMLParser

from carbuyer.sources.base import ListingRef, RawListing

_ID_RE = re.compile(r"/(\d+)(?:\?|$)")
_SELLER_RE = re.compile(r"ca-prod-(fsbo|dealer)-ads")
_SELLER_KIND = {"fsbo": "private", "dealer": "dealer"}


def parse_search_listings(html: str) -> list[RawListing]:
    """Every car in the page's schema.org ItemList, mapped to RawListing.
    Empty when the page has no ItemList (no results / blocked)."""
    out: list[RawListing] = []
    for entry in _item_list(html):
        listing = _car_to_listing(_as_dict(_as_dict(entry).get("item")))
        if listing is not None:
            out.append(listing)
    return out


def _item_list(html: str) -> list[Any]:
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(deep=True, strip=False))
        except (ValueError, TypeError):
            continue
        block = _as_dict(data)
        if block.get("@type") != "ItemList":
            continue
        elems = block.get("itemListElement")
        return cast("list[Any]", elems) if isinstance(elems, list) else []
    return []


def _car_to_listing(item: dict[str, Any]) -> RawListing | None:
    url = item.get("url")
    if not isinstance(url, str):
        return None
    m = _ID_RE.search(url)
    if m is None:  # no stable ad id -> can't key the upsert; drop it
        return None

    brand = _as_dict(item.get("brand"))
    odo = _as_dict(item.get("mileageFromOdometer"))
    offers = _as_dict(item.get("offers"))

    image = item.get("image")
    image = image if isinstance(image, str) else None
    sm = _SELLER_RE.search(image) if image else None
    city = _city_from_url(url)

    return RawListing(
        ref=ListingRef(source="kijiji", source_listing_id=m.group(1), url=url),
        title=_str(item.get("name")),
        description=_str(item.get("description")),
        photos=[image] if image else [],
        year=_to_int(item.get("vehicleModelDate")),
        make=_str(brand.get("name")),
        model=_str(item.get("model")),
        trim=_str(item.get("vehicleConfiguration")),
        mileage_km=_to_int(odo.get("value")),
        vin=_str(item.get("vehicleIdentificationNumber")),
        asking_price_cad=_cad_price(offers),
        seller_type=_SELLER_KIND.get(sm.group(1)) if sm else None,
        # ponytail: province needs a city->province map or a province-scoped
        # search; left None until want-matching needs it. City kept for later.
        location_province=None,
        extra={"city": city} if city else {},
    )


def _city_from_url(url: str) -> str | None:
    # /v-cars-trucks/<city>/<title-slug>/<id>
    parts = url.split("/v-cars-trucks/", 1)
    if len(parts) != 2:  # noqa: PLR2004
        return None
    tail = parts[1].split("/")
    return (tail[0] or None) if tail else None


def _cad_price(offers: dict[str, Any]) -> Decimal | None:
    if offers.get("priceCurrency") != "CAD":
        return None
    try:
        price = Decimal(str(offers.get("price")))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return price if price > 0 else None  # "0"/"Please Contact" -> no price


def _as_dict(obj: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", obj) if isinstance(obj, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
