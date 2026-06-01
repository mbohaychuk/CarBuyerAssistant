"""Kijiji response parsers.

Kijiji (kijiji.ca) is a Next.js + Apollo SPA. All listing data is embedded in a
single ``<script id="__NEXT_DATA__">`` JSON blob under
``props.pageProps.__APOLLO_STATE__``, keyed ``AutosListing:<id>``. We parse that
JSON rather than the rendered DOM — it survives cosmetic redesigns and carries
normalized attributes the visible page doesn't.

Two response shapes matter, and they are *complementary*:

  1. Search-results page (``/b-cars-trucks/<prov>/c174l<id>``) — many
     ``AutosListing`` records. Carries normalized ``caryear`` / ``carmake`` /
     ``carmodel`` / ``carmileageinkms`` attributes (authoritative for those), a
     **truncated** description, and the first ~5 photos.
  2. Listing-detail page (``/v-cars-trucks/.../<id>``) — one ``AutosListing``
     record. Carries the **full** description and **all** photos, but its
     structured year / mileage attributes are usually empty (seller-entered,
     not server-normalized).

So the caller (``KijijiSource``) must coalesce: take the long description and
full photo set from the detail page, and year / make / model / mileage from the
search page — neither page's gaps should clobber the other's data.

VIN is carried in a structured ``vin`` attribute on the **search** page for the
subset of listings whose seller entered one (the detail page has no attributes
at all). We read it when present; the enricher / ``find_carfax_url`` still
backfills from the free-text description for the rest.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from carbuyer.shared.logging import get_logger

_log = get_logger("sources.kijiji.parser")

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

# A 4-digit model year anywhere in the title. Only used as a fallback when the
# normalized ``caryear`` attribute is empty (the detail page); on the search
# page ``caryear`` is authoritative.
_TITLE_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# Canadian province / territory codes, used to validate the 2-letter token
# pulled out of ``location.address`` (some addresses omit the province, and a
# bare "St" / unit token must not be mistaken for one).
_PROVINCE_CODES = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"},
)
# "<city>, AB T2T 4S1" / "<addr>, Grande Prairie, AB, T8X 4G8" — the province is
# a 2-letter token following a comma. finditer + the whitelist above tolerates
# multi-comma addresses and ignores non-province pairs.
_PROVINCE_RE = re.compile(r",\s*([A-Z]{2})\b")


@dataclass(slots=True, frozen=True)
class KijijiListing:
    """One parsed Kijiji ``AutosListing`` record (search stub or detail page).

    ``make`` / ``model`` are the raw lowercase canonical attribute values
    (e.g. ``"jeep"``, ``"cherokee"``); the enricher normalizes casing. ``year``
    / ``mileage_km`` are best-effort from structured attributes (search page)
    falling back to the title (year only). ``province`` is parsed from the
    address and may be ``None`` — the source backfills it from the search
    province.
    """

    listing_id: str
    url: str
    title: str | None
    description: str | None
    photos: list[str]
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    mileage_km: int | None
    vin: str | None
    ask_price_cad: Decimal | None
    province: str | None
    city: str | None
    is_dealer: bool
    extra: dict[str, Any] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=dict,
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """Narrow an arbitrary JSON value to a string-keyed dict (``{}`` otherwise).

    Laundering through ``Any`` here is what keeps the rest of the parser
    pyright-clean: ``isinstance(x, dict)`` alone narrows ``Any`` to
    ``dict[Unknown, Unknown]`` (whose ``.get`` is "partially unknown"), so we
    cast once at the boundary and descend over ``dict[str, Any]`` thereafter.
    """
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list) else []


def _extract_apollo_state(html: str) -> dict[str, Any]:
    """Pull ``props.pageProps.__APOLLO_STATE__`` out of the page's __NEXT_DATA__.

    Returns ``{}`` for any page that isn't a parseable Kijiji Next.js document
    (empty body, anti-bot interstitial, JSON change) so callers degrade to "no
    listings" rather than raising.
    """
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        # A healthy Kijiji page always carries __NEXT_DATA__; its absence means
        # an anti-bot interstitial or a re-platform off Next.js. Surface it —
        # otherwise the scraper goes silently blind (every cycle yields zero).
        _log.warning("Kijiji page has no __NEXT_DATA__ block (anti-bot or re-platform?)")
        return {}
    try:
        data: Any = json.loads(match.group(1))
    except json.JSONDecodeError:
        _log.warning("Kijiji __NEXT_DATA__ block is not valid JSON")
        return {}
    page_props = _as_dict(_as_dict(data).get("props")).get("pageProps")
    state = _as_dict(_as_dict(page_props).get("__APOLLO_STATE__"))
    if not state:
        # __NEXT_DATA__ parsed but the Apollo state is gone — page shape changed.
        # (A legitimately empty result page still has a populated state with
        # ROOT_QUERY/Location and just no AutosListing keys, so this is anomalous.)
        _log.warning("Kijiji page has no __APOLLO_STATE__ (page shape changed?)")
    return state


def _attr(record: dict[str, Any], name: str) -> str | None:
    """First ``canonicalValues[0]`` for the named ``attributes.all`` entry."""
    for entry_raw in _as_list(_as_dict(record.get("attributes")).get("all")):
        entry = _as_dict(entry_raw)
        if entry.get("canonicalName") != name:
            continue
        values = _as_list(entry.get("canonicalValues"))
        if values:
            first = values[0]
            # Normalize "" -> None to match _str_or_none, so an empty canonical
            # value is caught by the valuator's make/model/year guard rather
            # than degrading into an empty comp query.
            return first if isinstance(first, str) and first else None
        return None
    return None


def _int_attr(record: dict[str, Any], name: str) -> int | None:
    raw = _attr(record, name)
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _parse_year(record: dict[str, Any], title: str | None) -> int | None:
    year = _int_attr(record, "caryear")
    if year is not None:
        return year
    if title:
        match = _TITLE_YEAR_RE.search(title)
        if match is not None:
            return int(match.group(0))
    return None


def _parse_price(record: dict[str, Any]) -> Decimal | None:
    """Fixed-amount asking price in dollars, or ``None``.

    ``price.amount`` is in **cents**. Non-``FIXED`` price types ("CONTACT",
    swap/trade, etc.) carry no amount and yield ``None``. A non-positive amount
    is treated as "no price". Absurd-but-positive amounts (sellers who type
    99999999) pass through unmodified — the valuator handles them.
    """
    price = _as_dict(record.get("price"))
    if price.get("type") != "FIXED":
        return None
    amount = price.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return None
    return Decimal(amount) / Decimal(100)


def _parse_province(address: str | None) -> str | None:
    if not address:
        return None
    for match in _PROVINCE_RE.finditer(address):
        code = match.group(1)
        if code in _PROVINCE_CODES:
            return code
    return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_one(record: dict[str, Any]) -> KijijiListing | None:
    """Build a ``KijijiListing`` from one ``AutosListing`` record.

    Returns ``None`` (caller logs + skips) when the record lacks the stable id
    or url — those are the upsert key, so a record without them is unusable.
    """
    listing_id = record.get("id")
    url = record.get("url")
    if not isinstance(listing_id, str) or not listing_id:
        return None
    if not isinstance(url, str) or not url:
        return None

    title = _str_or_none(record.get("title"))

    photos = [u for u in _as_list(record.get("imageUrls")) if isinstance(u, str)]

    poster_id = _as_dict(record.get("posterInfo")).get("posterId")
    is_dealer = isinstance(poster_id, str) and poster_id.startswith("COMMERCIAL")

    location = _as_dict(record.get("location"))
    city = _str_or_none(location.get("name"))
    address = _str_or_none(location.get("address"))

    extra: dict[str, Any] = {}
    if isinstance(poster_id, str):
        extra["poster_id"] = poster_id

    return KijijiListing(
        listing_id=listing_id,
        url=url,
        title=title,
        description=_str_or_none(record.get("description")),
        photos=photos,
        year=_parse_year(record, title),
        make=_attr(record, "carmake"),
        model=_attr(record, "carmodel"),
        trim=_attr(record, "cartrim"),
        mileage_km=_int_attr(record, "carmileageinkms"),
        vin=_attr(record, "vin"),
        ask_price_cad=_parse_price(record),
        province=_parse_province(address),
        city=city,
        is_dealer=is_dealer,
        extra=extra,
    )


def _autos_listings(html: str) -> list[dict[str, Any]]:
    state = _extract_apollo_state(html)
    return [
        _as_dict(value)
        for key, value in state.items()
        if key.startswith("AutosListing:") and isinstance(value, dict)
    ]


def parse_search_page(html: str) -> list[KijijiListing]:
    """Parse every ``AutosListing`` on a search-results page.

    Returns dealer **and** owner listings (each flagged via ``is_dealer``) — the
    source filters dealers and uses the total to drive pagination. An empty list
    means "no listings on this page" (past the last page, or an unparseable
    document).
    """
    out: list[KijijiListing] = []
    for record in _autos_listings(html):
        parsed = _parse_one(record)
        if parsed is None:
            _log.warning("skipping Kijiji listing record without id/url")
            continue
        out.append(parsed)
    return out


def parse_listing_detail(html: str) -> KijijiListing | None:
    """Parse the single ``AutosListing`` on a listing-detail page.

    Returns ``None`` when the page carries no listing record (unparseable
    document / removed listing) so the source can fall back to the search stub.
    """
    records = _autos_listings(html)
    if not records:
        return None
    return _parse_one(records[0])
