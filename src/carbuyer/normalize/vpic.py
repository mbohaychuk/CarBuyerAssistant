"""NHTSA vPIC make/model normalization (free, no API key).

Snaps an LLM/scraped model string to the canonical NHTSA spelling for a given
make + year so comp keys line up — "F150" / "F 150" → "F-150". Best-effort:
any network/parse failure, or no match, returns the input model unchanged so it
never blocks enrichment. The make is left to the caller (comps already match
make case-insensitively); only the model spelling is canonicalized.
"""
from __future__ import annotations

import httpx

from carbuyer.shared.logging import get_logger

log = get_logger("normalize.vpic")

_VPIC_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles"


def _key(s: str) -> str:
    """Loose comparison key — alphanumerics only, upper-cased."""
    return "".join(c for c in s.upper() if c.isalnum())


async def canonical_model(
    make: str | None,
    model: str | None,
    year: int | None,
    *,
    client: httpx.AsyncClient,
) -> str | None:
    """Return the NHTSA-canonical model spelling, or ``model`` unchanged."""
    if not make or not model or year is None:
        return model
    url = f"{_VPIC_BASE}/GetModelsForMakeYear/make/{make}/modelyear/{year}?format=json"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        results = resp.json().get("Results", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("vpic lookup failed", make=make, year=year, error=str(exc))
        return model
    target = _key(model)
    for row in results:
        name = row.get("Model_Name")
        if isinstance(name, str) and _key(name) == target:
            return name
    return model
