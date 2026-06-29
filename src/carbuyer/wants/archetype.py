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
from carbuyer.normalize.vpic import canonical_model
from carbuyer.wants.criteria import ModelSpec


async def expand_archetype(
    text: str,
    *,
    provider: ArchetypeProvider,
    client: httpx.AsyncClient,
) -> list[ModelSpec]:
    expansion = await provider.expand_archetype(text)
    specs: list[ModelSpec] = []
    for m in expansion.models:
        year = m.year_min if m.year_min is not None else m.year_max
        canonical = await canonical_model(m.make, m.model, year, client=client)
        specs.append(
            ModelSpec(
                make=m.make,
                model=canonical or m.model,
                year_min=m.year_min,
                year_max=m.year_max,
                trims=m.trims,
            )
        )
    return specs
