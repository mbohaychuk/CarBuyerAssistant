"""Expand a fuzzy want archetype into concrete, vPIC-normalized model specs.

The orchestration seam between the LLM (ArchetypeProvider) and storage
(WantCriteria.model_specs). Pure of the dashboard and of the OpenAI SDK — both
the provider and the httpx client are injected — so it unit-tests with stubs.

Provider errors propagate; the caller (dashboard) catches them to offer a
manual-entry fallback. An empty expansion returns an empty list.
"""
from __future__ import annotations

import httpx

from carbuyer.llm.base import ArchetypeProvider
from carbuyer.llm.schemas import ExpandedModel
from carbuyer.normalize.vpic import canonical_model


async def expand_archetype(
    text: str,
    *,
    provider: ArchetypeProvider,
    client: httpx.AsyncClient,
) -> list[ExpandedModel]:
    """Return LLM-expanded models with vPIC-canonical spellings and preserved reason."""
    expansion = await provider.expand_archetype(text)
    rows: list[ExpandedModel] = []
    for m in expansion.models:
        year = m.year_min if m.year_min is not None else m.year_max
        canonical = await canonical_model(m.make, m.model, year, client=client)
        rows.append(m.model_copy(update={"model": canonical or m.model}))
    return rows
