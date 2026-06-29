# Archetype Expander Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner type a fuzzy vehicle archetype, have `gpt-5-nano` expand it into concrete vPIC-normalized `(make, model, year-range, trim)` rows, review/edit them on the dashboard, and save one want with a shared price/condition rule.

**Architecture:** Storage extends `WantCriteria` additively with a `model_specs` list (no migration). The Python `matches()` predicate gains a `model_specs` OR-branch, which both `backfill_want` and the forward valuator path inherit for free. A new `ArchetypeProvider` LLM role + `wants/archetype.py` orchestrator produce the candidate specs; the dashboard `/wants` page hosts the expand → review → save flow.

**Tech Stack:** Python 3.13 asyncio, SQLAlchemy 2 async, Pydantic v2, FastAPI + Jinja2/HTMX, OpenAI `gpt-5-nano` via `chat.completions.parse`, NHTSA vPIC (httpx).

## Global Constraints

- Spec of record: `docs/specs/2026-06-29-archetype-expander-design.md`.
- **No DB migration** — both new `WantCriteria` fields default-empty so existing flat configs validate under `extra="forbid"`.
- TDD: write the failing test, run it red, implement minimally, run green, commit.
- Tests build schema via `Base.metadata.create_all`; DB tests use the session-scoped `engine` + per-test `session` fixtures in `tests/conftest.py`.
- Run a single test: `.venv/bin/python -m pytest <path>::<name> -v`. Full suite: `.venv/bin/python -m pytest -q`.
- Reuse, do not reimplement: `normalize/vpic.py::canonical_model`, `wants/repo.py::create_want`, `wants/service.py::backfill_want`, `llm/openai_provider.py::OpenAIProvider._parse_to`, `flags/taxonomy.py::DESIRABLE_TRIMS`.
- No AI attribution anywhere in code, commits, or docs.

---

### Task 1: `ModelSpec` + `WantCriteria` extension

**Files:**
- Modify: `src/carbuyer/wants/criteria.py`
- Test: `tests/wants/test_criteria.py`

**Interfaces:**
- Produces: `ModelSpec(make: str, model: str, year_min: int|None=None, year_max: int|None=None, trims: list[str]=[])` and two new `WantCriteria` fields `archetype_text: str|None=None`, `model_specs: list[ModelSpec]=[]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/wants/test_criteria.py`:

```python
from carbuyer.wants.criteria import ModelSpec, WantCriteria


def test_model_spec_round_trips_through_config() -> None:
    c = WantCriteria(
        archetype_text="cheap reliable 4runner-platform offroad",
        model_specs=[
            ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009, trims=[]),
            ModelSpec(make="Toyota", model="4Runner", year_min=2003, year_max=2009, trims=["SR5", "TRD"]),
        ],
        price_ceiling_cad=18000,
        provinces=["AB", "BC"],
    )
    dumped = c.model_dump(mode="json")
    restored = WantCriteria.model_validate(dumped)
    assert restored.archetype_text == "cheap reliable 4runner-platform offroad"
    assert len(restored.model_specs) == 2
    assert restored.model_specs[1].trims == ["SR5", "TRD"]


def test_legacy_flat_config_still_validates() -> None:
    # A want created before this feature has no archetype_text / model_specs.
    legacy = {"makes": ["Nissan"], "models": ["Xterra"], "transmissions": ["manual"]}
    c = WantCriteria.model_validate(legacy)
    assert c.makes == ["Nissan"]
    assert c.model_specs == []
    assert c.archetype_text is None


def test_model_spec_year_order_validated() -> None:
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ModelSpec(make="Lexus", model="GX 470", year_min=2010, year_max=2003)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/wants/test_criteria.py -k "model_spec or legacy_flat" -v`
Expected: FAIL with `ImportError: cannot import name 'ModelSpec'`.

- [ ] **Step 3: Implement `ModelSpec` + the two fields**

In `src/carbuyer/wants/criteria.py`, add `ModelSpec` above `WantCriteria` (after the imports):

```python
class ModelSpec(BaseModel):
    """One concrete model in an archetype's fan-out: a precise make+model with
    its own year range and trim hints. The matcher ORs across a want's specs."""
    model_config = ConfigDict(extra="forbid")

    make: str
    model: str
    year_min: int | None = None
    year_max: int | None = None
    trims: list[str] = []

    @model_validator(mode="after")
    def _year_range_ordered(self) -> ModelSpec:
        if (
            self.year_min is not None
            and self.year_max is not None
            and self.year_min > self.year_max
        ):
            raise ValueError("year_min must not be greater than year_max")
        return self
```

Then add the two fields to `WantCriteria` (just below `model_config`):

```python
    # Archetype fan-out (Phase 2). archetype_text is the original fuzzy text the
    # LLM expanded; model_specs is the confirmed concrete set. Both default-empty
    # so legacy flat wants validate unchanged. A want uses EITHER model_specs
    # (archetype) OR the flat makes/models (manual) for identity — not both.
    archetype_text: str | None = None
    model_specs: list[ModelSpec] = []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/wants/test_criteria.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/wants/criteria.py tests/wants/test_criteria.py
git commit -m "feat(wants): ModelSpec + archetype fields on WantCriteria"
```

---

### Task 2: matcher `model_specs` OR-branch

**Files:**
- Modify: `src/carbuyer/wants/matcher.py`
- Test: `tests/wants/test_matcher.py`, `tests/wants/test_coarse_prematch.py`

**Interfaces:**
- Consumes: `WantCriteria.model_specs`, `ModelSpec` (Task 1).
- Produces: unchanged public signatures `matches(lot, criteria, *, pickup_province, offer_price_cad) -> bool` and `could_match_any_want(*, make, model, year, title, criteria_list) -> bool`; new private helpers `_identity_ok`, `_spec_matches`, `_coarse_spec`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/wants/test_matcher.py`, **reusing the file's existing `_lot(**over) -> AuctionLot` helper** (defaults: Nissan Xterra 2010, trim=None, manual/4wd — override only what each test needs). Add `ModelSpec` to the existing `from carbuyer.wants.criteria import ...` line:

```python
def test_model_spec_matches_within_its_year_range() -> None:
    crit = WantCriteria(model_specs=[
        ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009),
        ModelSpec(make="Lexus", model="GX 460", year_min=2010, year_max=2019),
    ])
    assert matches(_lot(make="Lexus", model="GX 470", year=2005), crit) is True
    assert matches(_lot(make="Lexus", model="GX 460", year=2015), crit) is True


def test_model_spec_excludes_sibling_out_of_its_range() -> None:
    crit = WantCriteria(model_specs=[
        ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009),
    ])
    # A GX 470 from 2015 is out of THIS spec's range → no match.
    assert matches(_lot(make="Lexus", model="GX 470", year=2015), crit) is False


def test_model_spec_trims_scoped_per_spec() -> None:
    crit = WantCriteria(model_specs=[
        ModelSpec(make="Toyota", model="4Runner", year_min=2003, year_max=2009, trims=["TRD"]),
    ])
    assert matches(_lot(make="Toyota", model="4Runner", year=2005, trim="TRD"), crit) is True
    assert matches(_lot(make="Toyota", model="4Runner", year=2005, trim="SR5"), crit) is False
    # Lenient on unknown trim (a buyer-assistant would rather over-alert).
    assert matches(_lot(make="Toyota", model="4Runner", year=2005, trim=None), crit) is True


def test_flat_path_unchanged_when_no_specs() -> None:
    # Regression: a legacy flat want must behave exactly as before.
    crit = WantCriteria(makes=["Nissan"], models=["Xterra"], year_min=2005, year_max=2015)
    assert matches(_lot(make="Nissan", model="Xterra", year=2010), crit) is True
    assert matches(_lot(make="Toyota", model="Tacoma", year=2010), crit) is False
```

Add to `tests/wants/test_coarse_prematch.py`:

```python
def test_coarse_gate_ors_over_model_specs() -> None:
    from carbuyer.wants.criteria import ModelSpec, WantCriteria
    from carbuyer.wants.matcher import could_match_any_want
    crit = WantCriteria(model_specs=[
        ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009),
    ])
    assert could_match_any_want(
        make=None, model=None, year=2005,
        title="2005 Lexus GX 470 4x4", criteria_list=[crit],
    ) is True
    assert could_match_any_want(
        make=None, model=None, year=2005,
        title="2005 Toyota Camry", criteria_list=[crit],
    ) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/wants/test_matcher.py tests/wants/test_coarse_prematch.py -k "model_spec or flat_path_unchanged or coarse_gate_ors" -v`
Expected: FAIL — `matches` returns `True` for the sibling-out-of-range case (the flat path ignores `model_specs` today).

- [ ] **Step 3: Implement the OR-branch**

In `src/carbuyer/wants/matcher.py`, replace the identity rows inside `matches` and add the helpers. The new `matches` body:

```python
def matches(
    lot: VehicleOffer,
    criteria: WantCriteria,
    *,
    pickup_province: str | None = None,
    offer_price_cad: int | Decimal | None = None,
) -> bool:
    checks = (
        not (criteria.hide_showstoppers and lot.showstopper_flags),
        _identity_ok(lot, criteria),
        _in_set(lot.transmission, criteria.transmissions, lenient_unknown=True),
        _in_set(lot.drivetrain, criteria.drivetrains, lenient_unknown=True),
        _at_most(offer_price_cad, criteria.price_ceiling_cad),
        _at_most(lot.mileage_km, criteria.max_mileage_km),
        _province_ok(pickup_province, criteria.provinces),
        _condition_ok(
            lot.condition_categorical,
            criteria.condition_min,
            sparse=bool(lot.condition_inferred_from_sparse_listing),
        ),
    )
    return all(checks)


def _identity_ok(lot: VehicleOffer, criteria: WantCriteria) -> bool:
    """Vehicle identity (make/model/year/trim). model_specs (archetype) takes
    precedence and is OR'd across specs; otherwise the flat makes/models path."""
    if criteria.model_specs:
        return any(_spec_matches(lot, s) for s in criteria.model_specs)
    return (
        _in_set(lot.make, criteria.makes, lenient_unknown=False)
        and _in_set(lot.model, criteria.models, lenient_unknown=False)
        and _in_set(lot.trim, criteria.trims, lenient_unknown=True)
        and _year_in_range(lot.year, criteria.year_min, criteria.year_max)
    )


def _spec_matches(lot: VehicleOffer, spec: ModelSpec) -> bool:
    return (
        _in_set(lot.make, [spec.make], lenient_unknown=False)
        and _in_set(lot.model, [spec.model], lenient_unknown=False)
        and _year_in_range(lot.year, spec.year_min, spec.year_max)
        and _in_set(lot.trim, spec.trims, lenient_unknown=True)
    )
```

Add `ModelSpec` to the import from `carbuyer.wants.criteria`:

```python
from carbuyer.wants.criteria import ModelSpec, WantCriteria
```

Update `_coarse_match` to OR over specs:

```python
def _coarse_match(c: WantCriteria, year: int | None, hay: str) -> bool:
    if c.model_specs:
        return any(_coarse_spec(s, year, hay) for s in c.model_specs)
    if c.makes and not _any_term_in(c.makes, hay):
        return False
    if c.models and not _any_term_in(c.models, hay):
        return False
    return _year_ok(year, c.year_min, c.year_max)


def _coarse_spec(s: ModelSpec, year: int | None, hay: str) -> bool:
    if not _any_term_in([s.make], hay):
        return False
    if not _any_term_in([s.model], hay):
        return False
    return _year_ok(year, s.year_min, s.year_max)


def _year_ok(year: int | None, ymin: int | None, ymax: int | None) -> bool:
    if year is None:  # unknown year is lenient (kept)
        return True
    if ymin is not None and year < ymin:
        return False
    return not (ymax is not None and year > ymax)
```

(The `_year_ok` helper replaces the inline year check that was duplicated in the old `_coarse_match`; keep its existing year semantics.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/wants/ -v`
Expected: PASS (new + all pre-existing matcher/coarse tests — the flat-path regression confirms no behaviour drift).

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/wants/matcher.py tests/wants/test_matcher.py tests/wants/test_coarse_prematch.py
git commit -m "feat(wants): model_specs OR-branch in matcher + coarse gate"
```

---

### Task 3: `ArchetypeExpansion` schema + `ArchetypeProvider` role + `OpenAIProvider.expand_archetype`

**Files:**
- Modify: `src/carbuyer/llm/schemas.py`, `src/carbuyer/llm/base.py`, `src/carbuyer/llm/openai_provider.py`, `src/carbuyer/llm/prompts.py`
- Test: `tests/llm/test_schemas.py`, `tests/llm/test_prompts.py` (create if absent)

**Interfaces:**
- Produces: `ExpandedModel`, `ArchetypeExpansion(models: list[ExpandedModel])` (schemas); `ArchetypeProvider` ABC with `async def expand_archetype(self, text: str) -> ArchetypeExpansion`; `OpenAIProvider` now implements it; `archetype_system_prompt() -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/llm/test_schemas.py`:

```python
def test_archetype_expansion_round_trips() -> None:
    from carbuyer.llm.schemas import ArchetypeExpansion
    payload = {"models": [
        {"make": "Lexus", "model": "GX 470", "year_min": 2003, "year_max": 2009,
         "trims": [], "reason": "J120 4Runner platform, body-on-frame"},
    ]}
    exp = ArchetypeExpansion.model_validate(payload)
    assert exp.models[0].make == "Lexus"
    assert exp.models[0].reason
```

Create `tests/llm/test_prompts.py`:

```python
def test_archetype_prompt_seeds_from_taxonomy() -> None:
    from carbuyer.llm.prompts import archetype_system_prompt
    p = archetype_system_prompt()
    # Seeded with the desirable-trims taxonomy so the model knows platform
    # relationships; spot-check a known entry.
    assert "GX 470" in p or "GX470" in p
    assert "year" in p.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/llm/test_schemas.py::test_archetype_expansion_round_trips tests/llm/test_prompts.py -v`
Expected: FAIL — `ImportError` for `ArchetypeExpansion` / `archetype_system_prompt`.

- [ ] **Step 3: Implement schema, role, prompt, provider method**

In `src/carbuyer/llm/schemas.py` (near the other output models):

```python
class ExpandedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    make: str
    model: str
    year_min: int | None
    year_max: int | None
    trims: list[str]
    reason: str  # one line: why this model fits the archetype (shown in the table)


class ArchetypeExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[ExpandedModel]
```

In `src/carbuyer/llm/base.py`, import the schema and add the role (after `VisionProvider`):

```python
from carbuyer.llm.schemas import ArchetypeExpansion, EnrichmentOutput, VisionOutput
# ...
class ArchetypeProvider(_AsyncCM, ABC):
    name: str = "abstract"

    @abstractmethod
    async def expand_archetype(self, text: str) -> ArchetypeExpansion: ...
```

In `src/carbuyer/llm/prompts.py`, add (reusing the taxonomy seed pattern of `description_system_prompt`):

```python
from carbuyer.flags.taxonomy import DESIRABLE_TRIMS


def _desirable_trims_seed() -> str:
    lines = []
    for d in DESIRABLE_TRIMS:
        make = d.get("make", "")
        model = d.get("model", "")
        trim = d.get("trim", "")
        note = d.get("note", "")
        lines.append(f"- {make} {model} {trim}".rstrip() + (f" — {note}" if note else ""))
    return "\n".join(lines)


def archetype_system_prompt() -> str:
    return (
        "You expand a used-vehicle buyer's fuzzy archetype into concrete, "
        "North-American-market models with realistic model-year ranges and "
        "optional trim hints. Use enthusiast/platform knowledge (shared "
        "platforms, engine swaps, desirable trims) — e.g. a Lexus GX 470 is the "
        "J120 4Runner platform; an Xterra offroad want implies the manual "
        "Off-Road trim. Return only the genuinely-fitting models (the platform "
        "siblings a knowledgeable buyer would cross-shop), not every vehicle in "
        "the segment. Each model needs make, model, year_min, year_max, trims "
        "(empty list = any trim), and a one-line reason.\n\n"
        "Known desirable models for reference (seed, not a whitelist):\n"
        f"{_desirable_trims_seed()}"
    )
```

(`DESIRABLE_TRIMS` is a `list[dict]` with `make` / `model` / `trim` / `note` keys — confirmed at `flags/taxonomy.py:260`; the `.get()` accesses above are correct.)

In `src/carbuyer/llm/openai_provider.py`: add the token ceiling, import the schema + role + prompt, make `OpenAIProvider` implement the role, and add the method:

```python
# near the other *_MAX_TOKENS constants
ARCHETYPE_MAX_TOKENS = 1500
```

```python
from carbuyer.llm.base import (
    ArchetypeProvider,
    DescribeInput,
    LLMProvider,
    VisionInput,
)
from carbuyer.llm.prompts import (
    VISION_AGGREGATION_PROMPT,
    VISION_PER_IMAGE_PROMPT,
    archetype_system_prompt,
    description_system_prompt,
    description_user_prompt,
)
from carbuyer.llm.schemas import (
    ArchetypeExpansion,
    EnrichmentOutput,
    PerImageOutput,
    VisionOutput,
)
```

```python
class OpenAIProvider(LLMProvider, ArchetypeProvider):
    # ...existing __init__ etc...

    async def expand_archetype(self, text: str) -> ArchetypeExpansion:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": archetype_system_prompt()},
            {"role": "user", "content": text},
        ]
        return await self._parse_to(
            response_format=ArchetypeExpansion,
            messages=messages,
            max_tokens=ARCHETYPE_MAX_TOKENS,
            kind="archetype",
            lot_id=None,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/llm/test_schemas.py tests/llm/test_prompts.py -v && .venv/bin/pyright src/carbuyer/llm/`
Expected: PASS; pyright 0 errors (confirms `OpenAIProvider` satisfies `ArchetypeProvider`).

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/llm/
git commit -m "feat(llm): ArchetypeProvider role + gpt-5-nano archetype expansion"
```

---

### Task 4: `wants/archetype.py` — `expand_archetype` orchestrator (LLM + vPIC)

**Files:**
- Create: `src/carbuyer/wants/archetype.py`
- Test: `tests/wants/test_archetype.py`

**Interfaces:**
- Consumes: `ArchetypeProvider` (Task 3), `ModelSpec` (Task 1), `normalize.vpic.canonical_model`.
- Produces: `async def expand_archetype(text: str, *, provider: ArchetypeProvider, client: httpx.AsyncClient) -> list[ModelSpec]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/wants/test_archetype.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/wants/test_archetype.py -v`
Expected: FAIL — `ModuleNotFoundError: carbuyer.wants.archetype`.

- [ ] **Step 3: Implement the orchestrator**

Create `src/carbuyer/wants/archetype.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/wants/test_archetype.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/wants/archetype.py tests/wants/test_archetype.py
git commit -m "feat(wants): archetype expander orchestrator (LLM + vPIC)"
```

---

### Task 5: Dashboard expand → review → save + want-detail display

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/wants.py`
- Create: `src/carbuyer/apps/dashboard/templates/partials/want_specs.html`
- Modify: `src/carbuyer/apps/dashboard/templates/pages/wants.html`, `src/carbuyer/apps/dashboard/templates/pages/want_detail.html`
- Test: `tests/apps/dashboard/test_wants.py`

**Interfaces:**
- Consumes: `expand_archetype` (Task 4), `WantCriteria` + `ModelSpec` (Task 1), `repo.create_want`, `service.backfill_want`.
- Produces: `POST /wants/expand` (form `archetype_text`) → rows partial; extended `POST /wants` accepting `model_specs`; module function `_expand(text) -> list[ModelSpec]` (the monkeypatch seam for tests).

- [ ] **Step 1: Write the failing tests**

Add to `tests/apps/dashboard/test_wants.py`, following the file's existing harness:
the `_patch_deps` fixture (which IS the test session, with `get_session_maker` patched)
and the `_client(*, follow=True)` factory. `asyncio_mode = "auto"`, so **no
`@pytest.mark.asyncio` decorator** (match the existing tests).

```python
from carbuyer.wants.criteria import ModelSpec  # add to the existing imports


async def test_expand_endpoint_renders_rows(
    _patch_deps: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_expand(text: str) -> list[ModelSpec]:
        return [ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009, trims=[])]
    monkeypatch.setattr("carbuyer.apps.dashboard.routers.wants._expand", fake_expand)

    async with _client() as c:
        r = await c.post("/wants/expand", data={"archetype_text": "4runner platform"})
    assert r.status_code == 200  # noqa: PLR2004
    assert "GX 470" in r.text
    assert "2003" in r.text


async def test_expand_endpoint_handles_provider_error(
    _patch_deps: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(text: str) -> list[ModelSpec]:
        raise RuntimeError("openai down")
    monkeypatch.setattr("carbuyer.apps.dashboard.routers.wants._expand", boom)

    async with _client() as c:
        r = await c.post("/wants/expand", data={"archetype_text": "x"})
    assert r.status_code == 200  # noqa: PLR2004
    assert "manually" in r.text.lower()


async def test_save_archetype_want_persists_model_specs(_patch_deps: AsyncSession) -> None:
    session = _patch_deps
    async with _client(follow=False) as c:
        r = await c.post("/wants", data=[
            ("name", "4runner platform"),
            ("archetype_text", "cheap reliable 4runner-platform offroad"),
            ("spec_make", "Lexus"), ("spec_model", "GX 470"),
            ("spec_year_min", "2003"), ("spec_year_max", "2009"), ("spec_trims", ""),
            ("spec_include", "0"),
            ("max_price_cad", "18000"),
        ])
    assert r.status_code == 303  # noqa: PLR2004

    from carbuyer.wants import repo
    from carbuyer.wants.criteria import WantCriteria
    wants = await repo.list_wants(session)
    crit = WantCriteria.model_validate(wants[-1].config)
    assert crit.archetype_text == "cheap reliable 4runner-platform offroad"
    assert crit.model_specs == [
        ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009, trims=[])
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/apps/dashboard/test_wants.py -k "expand_endpoint or save_archetype" -v`
Expected: FAIL — `404` on `/wants/expand` (route absent); save test fails (handler ignores `spec_*`).

- [ ] **Step 3: Implement the router changes**

In `src/carbuyer/apps/dashboard/routers/wants.py`:

Add imports:

```python
import httpx
from carbuyer.llm.openai_provider import OpenAIProvider
from carbuyer.wants.archetype import expand_archetype
from carbuyer.wants.criteria import ModelSpec, WantCriteria, first_error
```

Add the expand seam + endpoint:

```python
async def _expand(text: str) -> list[ModelSpec]:
    """Build a request-scoped provider + http client and expand. The single
    monkeypatch seam for dashboard tests."""
    async with OpenAIProvider() as provider, httpx.AsyncClient() as client:
        return await expand_archetype(text, provider=provider, client=client)


@router.post("/wants/expand", response_class=HTMLResponse)
async def wants_expand(
    request: Request,
    _user: Annotated[CurrentUser, Depends(current_user)],
    archetype_text: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        specs = await _expand(archetype_text)
    except Exception:  # provider/network failure → manual fallback
        log.exception("archetype expansion failed")
        specs = []
        error = "Couldn't expand the archetype right now — add models manually below."
    else:
        error = None if specs else "No models matched — add them manually below."
    return templates.TemplateResponse(
        request, "partials/want_specs.html",
        {"specs": specs, "archetype_text": archetype_text, "error": error},
    )
```

Add a `log = get_logger("dashboard.wants")` at module top (import `get_logger` from `carbuyer.shared.logging`).

Add the spec-parsing helper:

```python
def _parse_model_specs(
    makes: list[str], models: list[str],
    year_mins: list[str], year_maxs: list[str], trims: list[str],
    include: list[str],
) -> list[ModelSpec]:
    """Zip the parallel per-row form arrays into ModelSpec, keeping only rows
    whose index is in `include` (the checked checkboxes)."""
    keep = {int(i) for i in include if i.strip().isdigit()}
    specs: list[ModelSpec] = []
    for i, make in enumerate(makes):
        if i not in keep or not make.strip() or not models[i].strip():
            continue
        specs.append(ModelSpec(
            make=make.strip(),
            model=models[i].strip(),
            year_min=_int_or_none(year_mins[i]) if i < len(year_mins) else None,
            year_max=_int_or_none(year_maxs[i]) if i < len(year_maxs) else None,
            trims=[t.strip() for t in (trims[i] if i < len(trims) else "").split(",") if t.strip()],
        ))
    return specs
```

Extend `wants_create` to accept the spec arrays + `archetype_text` and branch:

```python
@router.post("/wants", response_model=None)
async def wants_create(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    name: Annotated[str, Form()],
    makes: Annotated[str | None, Form()] = None,
    models: Annotated[str | None, Form()] = None,
    trims: Annotated[str | None, Form()] = None,
    transmissions: Annotated[str | None, Form()] = None,
    drivetrains: Annotated[str | None, Form()] = None,
    year_min: Annotated[str | None, Form()] = None,
    year_max: Annotated[str | None, Form()] = None,
    max_price_cad: Annotated[str | None, Form()] = None,
    max_mileage_km: Annotated[str | None, Form()] = None,
    provinces: Annotated[str | None, Form()] = None,
    condition_min: Annotated[str | None, Form()] = None,
    archetype_text: Annotated[str | None, Form()] = None,
    spec_make: Annotated[list[str], Form()] = [],
    spec_model: Annotated[list[str], Form()] = [],
    spec_year_min: Annotated[list[str], Form()] = [],
    spec_year_max: Annotated[list[str], Form()] = [],
    spec_trims: Annotated[list[str], Form()] = [],
    spec_include: Annotated[list[str], Form()] = [],
) -> HTMLResponse | RedirectResponse:
    try:
        model_specs = _parse_model_specs(
            spec_make, spec_model, spec_year_min, spec_year_max, spec_trims, spec_include,
        )
        if model_specs:
            # Archetype want: specs carry identity; flat make/model stay empty.
            criteria = WantCriteria(
                archetype_text=(archetype_text or None),
                model_specs=model_specs,
                transmissions=[t.strip().lower() for t in (transmissions or "").split(",") if t.strip()],
                drivetrains=[d.strip().lower() for d in (drivetrains or "").split(",") if d.strip()],
                price_ceiling_cad=_int_or_none(max_price_cad),
                max_mileage_km=_int_or_none(max_mileage_km),
                provinces=[p.strip() for p in (provinces or "").split(",") if p.strip()],
                condition_min=((condition_min or "").strip().lower() or None),
            )
        else:
            criteria = WantCriteria.from_inputs(
                makes=makes, models=models, trims=trims,
                transmissions=transmissions, drivetrains=drivetrains,
                year_min=_int_or_none(year_min), year_max=_int_or_none(year_max),
                max_price_cad=_int_or_none(max_price_cad),
                max_mileage_km=_int_or_none(max_mileage_km),
                provinces=provinces, condition_min=condition_min,
            )
        want = await repo.create_want(session, name=name, criteria=criteria)
        await service.backfill_want(session, want)
    except ValidationError as exc:
        return await _render_list(request, session, error=f"Invalid want: {first_error(exc)}")
    except ValueError as exc:
        return await _render_list(request, session, error=f"Invalid want: {exc}")
    await session.commit()
    return RedirectResponse("/wants", status_code=303)
```

- [ ] **Step 4: Add the templates**

Create `src/carbuyer/apps/dashboard/templates/partials/want_specs.html`:

```html
{% if error %}<p class="muted">{{ error }}</p>{% endif %}
<input type="hidden" name="archetype_text" value="{{ archetype_text or '' }}">
<table class="spec-table">
  <thead><tr><th></th><th>Make</th><th>Model</th><th>Yr min</th><th>Yr max</th><th>Trims</th><th>Why</th></tr></thead>
  <tbody>
    {% for s in specs %}
      <tr>
        <td><input type="checkbox" name="spec_include" value="{{ loop.index0 }}" checked></td>
        <td><input name="spec_make" value="{{ s.make }}"></td>
        <td><input name="spec_model" value="{{ s.model }}"></td>
        <td><input name="spec_year_min" type="number" value="{{ s.year_min if s.year_min is not none else '' }}"></td>
        <td><input name="spec_year_max" type="number" value="{{ s.year_max if s.year_max is not none else '' }}"></td>
        <td><input name="spec_trims" value="{{ s.trims | join(', ') }}"></td>
        <td class="muted">{{ s.reason if s.reason is defined else '' }}</td>
      </tr>
    {% endfor %}
  </tbody>
</table>
```

In `pages/wants.html`, add an archetype block above the existing manual form (the expand result is injected into `#spec-rows`; both the rows and the shared-rule fields submit together to `/wants`):

```html
  <form method="post" action="/wants" class="want-form">
    <input name="name" placeholder="Name (e.g. cheap 4Runner-platform offroad)" required>
    <div class="archetype-row">
      <input name="archetype_text" id="archetype-text"
             placeholder="Describe an archetype (e.g. reliable body-on-frame offroad under $18k)">
      <button type="button"
              hx-post="/wants/expand"
              hx-include="[name='archetype_text']"
              hx-target="#spec-rows" hx-swap="innerHTML">Expand →</button>
    </div>
    <div id="spec-rows"></div>
    <input name="max_price_cad" type="number" placeholder="Max price $">
    <input name="provinces" placeholder="Provinces (AB, BC)">
    <input name="condition_min" placeholder="Min condition (decent, good)">
    <button type="submit">Save want</button>
  </form>
```

(Keep the existing flat manual form below as a second `<form>` for non-archetype wants, unchanged.)

In `pages/want_detail.html`, show the archetype text under the heading:

```html
  <h1>{{ want.name }}</h1>
  {% set crit = want.config %}
  {% if crit.get('archetype_text') %}
    <p class="muted">expanded from: “{{ crit['archetype_text'] }}”</p>
  {% endif %}
```

- [ ] **Step 5: Run tests + build CSS check**

Run: `.venv/bin/python -m pytest tests/apps/dashboard/test_wants.py -v`
Expected: PASS (expand-renders, provider-error-fallback, save-persists-specs).
Then full suite: `.venv/bin/python -m pytest -q` → all green.

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/apps/dashboard/ tests/apps/dashboard/test_wants.py
git commit -m "feat(dashboard): archetype expand/review/save flow on /wants"
```

---

## Verification (after all tasks)

- [ ] Full suite green: `.venv/bin/python -m pytest -q`
- [ ] Types: `.venv/bin/pyright src/carbuyer/wants/ src/carbuyer/llm/ src/carbuyer/apps/dashboard/routers/wants.py`
- [ ] Lint: `.venv/bin/ruff check src/carbuyer/wants/ src/carbuyer/llm/ src/carbuyer/apps/dashboard/`
- [ ] Manual smoke (optional, needs OPENAI_API_KEY + DB): run the dashboard, open `/wants`, type an archetype, Expand, confirm rows render and Save creates a want with matches backfilled.
- [ ] `reuse-reviewer` agent on the diff before merge (per repo convention).
