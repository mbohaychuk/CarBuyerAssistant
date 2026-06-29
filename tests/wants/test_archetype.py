from __future__ import annotations

import httpx
import pytest

from carbuyer.llm.schemas import ArchetypeExpansion, ExpandedModel
from carbuyer.wants.archetype import expand_archetype
from carbuyer.wants.criteria import ModelSpec


class _StubProvider:
    def __init__(self, expansion: ArchetypeExpansion | Exception) -> None:
        self._expansion = expansion

    async def expand_archetype(self, text: str) -> ArchetypeExpansion:
        if isinstance(self._expansion, Exception):
            raise self._expansion
        return self._expansion


async def test_expand_normalizes_each_model_via_vpic(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_canonical(make, model, year, *, client):  # noqa: ANN001, ANN202
        return "GX 470" if model == "GX470" else model
    monkeypatch.setattr("carbuyer.wants.archetype.canonical_model", fake_canonical)

    provider = _StubProvider(ArchetypeExpansion(models=[
        ExpandedModel(make="Lexus", model="GX470", year_min=2003, year_max=2009,
                      trims=[], reason="J120 4Runner platform"),
    ]))
    async with httpx.AsyncClient() as client:
        specs = await expand_archetype("4runner platform offroad", provider=provider, client=client)

    assert specs == [ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009, trims=[])]


async def test_expand_empty_expansion_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _StubProvider(ArchetypeExpansion(models=[]))
    async with httpx.AsyncClient() as client:
        specs = await expand_archetype("nonsense", provider=provider, client=client)
    assert specs == []


async def test_expand_propagates_provider_error() -> None:
    provider = _StubProvider(RuntimeError("openai down"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError):
            await expand_archetype("anything", provider=provider, client=client)
