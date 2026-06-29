"""NHTSA reliability signal (free, no API key).

Pulls recall-campaign and consumer-complaint counts for a make/model/year from
NHTSA's public APIs — the "known issues" hard signal for the reliable /
easy-to-fix archetype. Best-effort: any network/parse failure returns None for
that count, so it never blocks enrichment. Counts only (not the full text) keep
the payload + the schema small.
"""
from __future__ import annotations

from typing import cast

import httpx

from carbuyer.shared.logging import get_logger

log = get_logger("normalize.nhtsa")

_RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"
_COMPLAINTS_URL = "https://api.nhtsa.gov/complaints/complaintsByVehicle"


async def _count(
    client: httpx.AsyncClient, url: str, params: dict[str, str | int],
) -> int | None:
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("nhtsa lookup failed", url=url, error=str(exc))
        return None
    if not isinstance(payload, dict):
        return None
    data = cast("dict[str, object]", payload)
    # The recalls endpoint returns "Count", complaints returns "count"; fall
    # back to the length of the results array if neither is an int.
    count = data.get("count", data.get("Count"))
    if isinstance(count, int):
        return count
    results = data.get("results")
    if isinstance(results, list):
        return len(cast("list[object]", results))
    return None


async def fetch_reliability(
    make: str | None,
    model: str | None,
    year: int | None,
    *,
    client: httpx.AsyncClient,
) -> tuple[int | None, int | None]:
    """Return (recall_count, complaint_count) for the vehicle, or Nones."""
    if not make or not model or year is None:
        return None, None
    params: dict[str, str | int] = {"make": make, "model": model, "modelYear": year}
    recalls = await _count(client, _RECALLS_URL, params)
    complaints = await _count(client, _COMPLAINTS_URL, params)
    return recalls, complaints
