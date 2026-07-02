"""Parse the Craigslist search JSON API (``postings/search/full``) into RawListings.

The response packs each posting as a variable-length array decoded against
``data.decode``: posting id = ``minPostingId + item[0]``, price = ``item[3]``,
location = ``item[4]`` ("subareaIdx:hoodIdx~lat~lon"), title = the last element,
plus optional tagged sublists (tag 6 = url slug, tag 9 = odometer). The detail URL
is rebuilt from region + subarea (``decode.locations[subareaIdx][2]``) + slug + id; a
posting whose subarea can't be decoded uses the region-only URL, which craigslist
redirects to the canonical one.

Only by-owner (``purveyor=owner``) results are ingested, so ``seller_type`` is
always 'private'. Province is passed in from the region searched (the API gives
only lat/lon). year/make/model/VIN are absent from search results — the enricher
normalizes them from the title. Legal: store metadata + the deep-link only, never
photos (craigslist v. 3Taps / Trader v. CarGurus) or seller PII.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, cast

from carbuyer.sources.base import ListingRef, RawListing

_MIN_LEN = 6  # fixed head occupies indices 0..5 (pid, _, cat, price, location, token)


def parse_search_results(
    payload: dict[str, Any], *, region: str, province: str | None,
) -> list[RawListing]:
    data = _as_dict(payload.get("data"))
    decode = _as_dict(data.get("decode"))
    min_pid_raw = decode.get("minPostingId")
    min_pid = min_pid_raw if isinstance(min_pid_raw, int) else 0
    locations = _as_list(decode.get("locations"))

    out: list[RawListing] = []
    for item in _as_list(data.get("items")):
        listing = _item_to_listing(item, min_pid, locations, region, province)
        if listing is not None:
            out.append(listing)
    return out


def _item_to_listing(
    item: Any, min_pid: int, locations: list[Any], region: str, province: str | None,
) -> RawListing | None:
    if not isinstance(item, list):
        return None
    row = cast("list[Any]", item)
    if len(row) < _MIN_LEN or not isinstance(row[0], int):
        return None
    slug = _tagged(row, 6)
    if not isinstance(slug, str) or not slug:  # no slug -> can't build a URL; drop it
        return None
    posting_id = min_pid + row[0]
    odo = _tagged(row, 9)
    title = row[-1] if isinstance(row[-1], str) else None
    return RawListing(
        ref=ListingRef(
            source="craigslist",
            source_listing_id=str(posting_id),
            url=_post_url(region, _subarea(row[4], locations), slug, posting_id),
        ),
        title=title,
        description=None,  # search API has none; the enricher fills from the title
        photos=[],  # legal: never store photos
        asking_price_cad=_price(row[3]),
        mileage_km=odo if isinstance(odo, int) else None,
        seller_type="private",  # purveyor=owner
        location_province=province,
        extra={},
    )


def _tagged(item: list[Any], tag: int) -> Any:
    for raw in item:
        if not isinstance(raw, list):
            continue
        el = cast("list[Any]", raw)
        if len(el) >= 2 and el[0] == tag:  # noqa: PLR2004 -- [tag, value]
            return el[1]
    return None


def _subarea(loc: Any, locations: list[Any]) -> str | None:
    if not isinstance(loc, str):
        return None
    parts = loc.split("~", 1)[0].split(":")  # "subareaIdx:hoodIdx"
    if len(parts) != 2 or not parts[0].isdigit():  # noqa: PLR2004
        return None
    idx = int(parts[0])  # first index selects the subarea; second is the neighborhood
    if 0 <= idx < len(locations):
        entry = locations[idx]
        if isinstance(entry, list):
            row = cast("list[Any]", entry)
            sub = row[2] if len(row) >= 3 else None  # noqa: PLR2004
            return sub if isinstance(sub, str) and sub else None
    return None


def _post_url(region: str, subarea: str | None, slug: str, posting_id: int) -> str:
    seg = f"{subarea}/" if subarea else ""
    return f"https://{region}.craigslist.org/{seg}cto/d/{slug}/{posting_id}.html"


def _price(value: Any) -> Decimal | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_dict(obj: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", obj) if isinstance(obj, dict) else {}


def _as_list(obj: Any) -> list[Any]:
    return cast("list[Any]", obj) if isinstance(obj, list) else []
