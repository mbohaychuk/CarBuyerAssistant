from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_LOT_MODELS_START = re.compile(r"var\s+lotModels\s*=\s*\[")

# Heuristic: any timestamp larger than this many seconds is treated as
# milliseconds. 1e11 seconds = March 1973; HiBid emits both seconds-since-epoch
# (recent dates) and milliseconds.
_MS_THRESHOLD = 1e11


def extract_lot_models(html: str) -> list[dict[str, Any]]:
    """Extract the embedded `var lotModels = [...];` array from a HiBid page.

    Uses a string-aware balanced-bracket scan rather than a non-greedy regex,
    because lot objects embed nested arrays (images, categories) which a
    simple `\\[.*?\\]` regex would truncate at the first inner `]`.
    """
    m = _LOT_MODELS_START.search(html)
    if not m:
        return []
    start = m.end() - 1  # index of the opening `[`
    depth = 0
    in_str = False
    escape = False
    quote = ""
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == quote:
                in_str = False
            continue
        if c in ('"', "'"):
            in_str = True
            quote = c
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                blob = html[start : i + 1]
                try:
                    parsed: object = json.loads(blob)
                except json.JSONDecodeError:
                    return []
                if not isinstance(parsed, list):
                    return []
                # Narrow each element to dict; drop anything else.
                return [item for item in parsed if isinstance(item, dict)]  # type: ignore[reportUnknownVariableType]
    return []


# Keys we copy from raw lotModels entries into RawLot.extra so the valuator and
# soft-close detector can use them without re-scraping.
_EXTRA_KEYS = (
    "bidIncrement",
    "reserveStatus",
    "buyNowPrice",
    "lotState",
    "saleType",
    "shippingOffered",
    "auctionCity",
    "auctionState",
    "companyName",
)


@dataclass(slots=True)
class HibidLotSummary:
    source_lot_id: str
    lot_number: str | None
    title: str | None
    description: str | None
    # year/make/model are NOT separate fields in HiBid lotModels; the description
    # enricher (Phase 3) parses them out of the title text.
    year: int | None
    make: str | None
    model: str | None
    current_high_bid_cad: Decimal | None
    bid_count_visible: int | None
    photos: list[str]
    end_at: datetime | None
    auction_external_id: str | None
    url: str | None
    extra: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]


def _get(obj: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def raw_lot_id(raw: dict[str, Any]) -> str | None:
    """Return the lot id from a raw lotModels entry, falling through key variants."""
    value = _get(raw, "eventItemId", "lotId", "lotID", "id")
    if value is None or value == "":
        return None
    return str(value)


def _to_decimal(v: Any) -> Decimal | None:  # noqa: PLR0911
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        cleaned = re.sub(r"[^\d.\-]", "", v)
        if not cleaned or cleaned in {"-", "."}:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _to_int(v: Any) -> int | None:  # noqa: PLR0911
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _parse_dt(value: Any) -> datetime | None:
    """Parse the various date shapes HiBid emits, always returning UTC-aware."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > _MS_THRESHOLD else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                dt = datetime.strptime(value, fmt)
            except ValueError:
                continue
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    return None


def _extract_photos(raw: dict[str, Any]) -> list[str]:
    photos_raw = _get(raw, "lotImages", "images")
    if not isinstance(photos_raw, list):
        return []
    items: list[Any] = photos_raw  # type: ignore[reportUnknownVariableType]
    photos: list[str] = []
    for p in items:
        if isinstance(p, str):
            photos.append(p)
        elif isinstance(p, dict):
            entry: dict[str, Any] = p  # type: ignore[reportUnknownVariableType]
            url = entry.get("url") or entry.get("imageUrl") or entry.get("largeUrl")
            if isinstance(url, str):
                photos.append(url)
    return photos


def parse_lot_summary(raw: dict[str, Any]) -> HibidLotSummary:
    lot_status_raw = raw.get("lotStatus")
    if isinstance(lot_status_raw, dict):
        lot_status: dict[str, Any] = lot_status_raw  # type: ignore[reportUnknownVariableType]
    else:
        lot_status = {}
    bid = _get(lot_status, "highBid", "currentBid") if lot_status else None
    if bid is None:
        bid = _get(raw, "highBid", "currentBid", "bidAmount")
    bid_count = _get(lot_status, "bidCount") if lot_status else None
    end = _get(raw, "auctionEnd", "saleEnd", "endTime", "scheduledEnd")
    extra: dict[str, Any] = {
        k: raw[k] for k in _EXTRA_KEYS if k in raw and raw[k] is not None
    }
    return HibidLotSummary(
        source_lot_id=raw_lot_id(raw) or "",
        lot_number=str(_get(raw, "lotNumber", "lotNum") or "") or None,
        title=_get(raw, "lead", "title", "lotTitle"),
        description=_get(raw, "description", "longDescription"),
        year=None,
        make=None,
        model=None,
        current_high_bid_cad=_to_decimal(bid),
        bid_count_visible=_to_int(bid_count),
        photos=_extract_photos(raw),
        end_at=_parse_dt(end),
        auction_external_id=str(_get(raw, "auctionId", "auctionID") or "") or None,
        url=_get(raw, "lotUrl", "url"),
        extra=extra,
    )
