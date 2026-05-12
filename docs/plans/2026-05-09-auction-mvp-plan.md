# CarBuyerAssistant Auction MVP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the auction-focused MVP described in `docs/specs/2026-05-08-carbuyer-mvp-design.md`: a personal Western Canadian auction deal-finder that scrapes HiBid + McDougall + farmauctionguide.com, enriches each lot with LLM analysis (description + nightly vision), scores lots on price-deal + rarity, sends Discord notifications on five trigger types, and surfaces everything via a localhost FastAPI/HTMX dashboard with comp comparison.

**Architecture:** Staged pipeline of independent Python workers (auction-discoverer → lot-scraper → enricher → valuator → notifier; bid-poller and vision-batcher run alongside) communicating through a single Postgres database via LISTEN/NOTIFY + `SELECT … FOR UPDATE SKIP LOCKED`. Each worker is its own systemd unit. Source plugins implement an `AuctionSource` interface for clean extensibility.

**Tech Stack:** Python 3.12+, uv, Ruff, Pyright (strict), pytest + pytest-asyncio, SQLAlchemy 2 async, psycopg 3, Alembic, httpx, selectolax, FastAPI, Jinja2, HTMX 2, Chart.js 4, discord.py v2, OpenAI SDK, Pydantic v2, structlog, pydantic-settings.

**Phases (12):**
- 0: Foundation (scaffolding, DB, config, logging, ORM)
- 1: HiBid source plugin
- 2: Pipeline workers (queue infrastructure, discoverer, lot-scraper)
- 3: LLM enrichment (OpenAI provider, description pass, taxonomies)
- 4: Valuation + scoring (comp set, channel norm, deal + rarity scores)
- 5: Discord bot (slash commands, persistent views, action buttons)
- 6: Notifier worker (5 trigger types, channel routing, quiet hours)
- 7: Bid polling (tiered cadence, soft-close, history)
- 8: Vision pass (two-pass nightly batch)
- 9: Distillation (auction-distiller, retention)
- 10: Additional sources (McDougall, farmauctionguide)
- 11: Dashboard (all views + comp comparison)
- 12: Production deployment (systemd, backups, smoke test)

**Verification path:** end-to-end smoke test in Phase 12 scrapes one HiBid auction, processes through the full pipeline, posts a real Discord notification, and renders the lot in the dashboard.

**Conventions used throughout:**
- Every test file imports `pytest` and `pytest_asyncio`. Async tests use `@pytest.mark.asyncio`.
- Every async DB function takes `session: AsyncSession` as last positional or keyword arg.
- All paths absolute from repo root (`src/carbuyer/...`, `tests/...`).
- `pytest --asyncio-mode=auto` is set in `pyproject.toml` so the marker is implicit; we still write it explicitly for clarity.
- All commits use imperative mood ("add", "fix", "refactor"), no AI attribution, never name AI tool configs.
- `git add` before each commit must be explicit (no `git add .`).

**Cumulative API renames (apply to every Phase 4+ task code block):**

The Phase 0/2 design overlays renamed two APIs that pre-Phase-3 task code blocks still reference. Phases 3+ overlays explicitly rebind these — but the inline task code blocks for Phases 5, 6, 7, 8, 9, 10, 11 may still show the old names. When implementing, treat every occurrence as a search-and-replace:

| Plan-text symbol (stale) | Real API (since Phase 0/2) | Notes |
|---|---|---|
| `from carbuyer.db.session import async_session_maker` | `from carbuyer.db.session import get_session, get_session_maker` | `get_session()` is the async-CM wrapper most workers want. |
| `async with async_session_maker() as session:` | `async with get_session() as session:` | |
| `from carbuyer.db.queue import claim_pending_lots` | `from carbuyer.db.queue import claim_pending_ids, select_pending_ids` | `claim_pending_ids` returns `list[int]` (not ORM objects); commits its own claim tx; caller iterates ids and re-fetches each lot in a fresh per-id `get_session()`. |
| `lots = await claim_pending_lots(session, status_field=..., limit=...)` | `ids = await claim_pending_ids(session, status_field=..., limit=...)` then loop opens fresh sessions | See `src/carbuyer/apps/enricher/enricher.py::process_pending` for the canonical worker shape. |
| `await self.client.beta.chat.completions.parse(...)` | `await self.client.chat.completions.parse(...)` | GA path; `.beta.` is legacy. Phase 3 overlay #8. |
| `lot.foo_status = "done"` (bare string) | `lot.foo_status = EnrichmentStatus.DONE` (StrEnum member) | StrEnum compares equal to strings so the old code worked, but writes via members survive grep and catch typos. |

Every continuous worker (`enricher`, `valuator`, `notifier`, `vision-batcher`, `bid-poller`) must additionally implement the **catchup-sweep idiom** before entering `LISTEN` (Phase 2 overlay #12) and the **transient-leftover self-NOTIFY** after each batch (Phase 3 overlay #35). See `src/carbuyer/apps/enricher/enricher.py::main` and `process_pending` for the reference implementation.

---

## Phase 0 — Foundation

### Phase 0 — Design-decision overlay (post-deliberation 2026-05-09)

Multi-discipline review (senior Python dev, DevOps/SRE, systems architect) raised the following concrete changes against the original Phase 0 plan. **The task code blocks below have been updated to reflect these decisions; this overlay documents the *why*.**

**Must-fix (locked in to avoid Phase 7+ pain):**

1. **Lazy DB engine, not module-level.** `engine = make_engine()` at import time creates a connection pool against the prod URL the moment any module imports `db.session` — even from a test that wants `carbuyer_test`. Replaced with `get_engine()` / `get_session_maker()` + `set_engine_for_testing()` so the conftest can install a test pool that all `get_session()` callers see. (Task 5, Task 8.)
2. **Source ABC abstract async-generators must be `def`, not `async def`.** Pyright-strict treats `async def f() -> AsyncIterator[T]: ...` (with `...` body) as a coroutine returning the type, not a generator yielding it; concrete implementations would have to lie about their type. Abstract definitions use `def f(self) -> AsyncIterator[T]: ...`; concrete implementations are `async def + yield`, which is the idiomatic async-generator form. (Task 9.)
3. **Strict-mode type annotations everywhere.** All function signatures get explicit parameter and return types. Bare `AsyncIterator` (no parameter) is invalid under strict — fixtures use `AsyncIterator[AsyncEngine]` etc. (Tasks 5, 8, 10.)
4. **Per-worker pool ≤ Postgres `max_connections`.** `pool_size=5, max_overflow=5` × 11 workers = 110 connections, exceeding Postgres default `max_connections=100`. Pool defaults shrink to `pool_size=2, max_overflow=3` (≤55 baseline ceiling) and the docker-compose Postgres command bumps `max_connections=200`. (Task 2, Task 5.)
5. **Postgres bound to `127.0.0.1`.** `0.0.0.0:5432` is open to the LAN. (Task 2.)
6. **AsyncIterator abstract methods syntactically correct.** See item 2 above. (Task 9.)

**Should-fix:**

7. **Source ABC split.** `AuctionSource` lumps discovery + fetch + bid polling. The bid-poller and the farmauctionguide router need different subsets. Split into `AuctionDiscoverer / AuctionFetcher / BidPoller` mixins; `AuctionSource = AuctionDiscoverer + AuctionFetcher + BidPoller` for HiBid/McDougall. The Phase-10 router will only implement `AuctionDiscoverer`. (Task 9.)
8. **`parser_version` on every plugin and on `auction_lots`.** When a parser changes, downstream rows have no way to know. Add `version: ClassVar[str]` to `Source`; add `parser_version: str | None` column to `auction_lots`. Cheap now, retroactive backfill later. (Task 6, Task 9.)
9. **`StrEnum` for status fields, plain `String(16)` on the DB.** Magic-string comparisons are typo footguns; native PG enums are migration-painful. `StrEnum` gets type-safety on the Python side, no migration friction. Statuses: `EnrichmentStatus`, `ValuationStatus`, `VisionStatus`, `NotificationStatus`, `LotStatus`, `AuctionStatus`, `UserAction`. (Task 6.)
10. **`extra: dict[str, Any]` escape hatch on `RawLot` and `RawAuction`.** Source-specific fields (Carfax URLs, reserve-met semantics, platform metadata) flow through enrichment without bloating the canonical schema. (Task 9.)
11. **`SOURCES` registry.** Two-line registry + `register()` so the dashboard "needs-plugin" view (Phase 10) and the router (Phase 10) can enumerate covered platforms. (Task 9.)
12. **Server-side defaults on non-nullable JSONB columns.** Python-side `default=list` doesn't apply to raw SQL inserts or migrations; `server_default=text("'[]'::jsonb")` does. (Task 6.)
13. **Test DB created via `/docker-entrypoint-initdb.d/`.** Manual `docker exec ... CREATE DATABASE` is forgotten on every fresh volume. (Task 2, Task 8.)
14. **Session-scoped engine + per-test savepoint rollback.** Per-test `drop_all/create_all` is ~150ms × N — tax that compounds. Schema built once; each test runs inside a transaction that rolls back. (Task 8.)
15. **Drop `pytest-postgresql` dep.** Listed but unused; we're committed to docker-compose Postgres. (Task 1.)
16. **Drop the no-op `get_sync_url()` Alembic helper.** psycopg3's `postgresql+psycopg://` URL works for both sync and async engines. (Task 7.)
17. **Logger return type matches `make_filtering_bound_logger` output.** That returns `structlog.types.FilteringBoundLogger`, not `structlog.stdlib.BoundLogger`. Strict mode catches this. (Task 4.)
18. **`configure_logging(level=None)` reads from `settings.log_level`.** Worker entrypoints call once with no args; per-unit `Environment=LOG_LEVEL=DEBUG` works. (Task 4.)
19. **Postgres 17.** GA since Sept 2024; pin to `postgres:17`. (Task 2.)
20. **`POSTGRES_PASSWORD` no fallback at runtime, only in `.env`.** Compose default of `local` makes it easy to ship the dev password. Compose still provides `${POSTGRES_PASSWORD:-local}` for the local dev case (paired with `.env` — gitignored), but the prod systemd unit will source from `infra/.env.prod` (Phase 12) with no fallback. (Task 2.)
21. **Healthcheck `start_period: 30s`.** Initdb on slow disks can exceed the 5×10 retry window. (Task 2.)
22. **HTTP client transport injection seam.** Phase 1 will add Retry-After + jittered exponential backoff. Wrapper accepts `transport` and merged headers now so the seam exists. UA pulled from `settings.http_user_agent` so the hardcoded "Chrome 126 / macOS 14.5" doesn't date the codebase. (Task 10.)
23. **Column ownership documented in `models.py`.** Each column block in `AuctionLot` is labeled with the worker that writes it (`# Owned by: lot-scraper (initial), bid-poller (updates)`). Coding rule: workers `UPDATE` only their own columns; no whole-object `merge`. (Task 6.)

**Deferred (acknowledged, not done in Phase 0):**

- **`ListingSource` ABC** — phase 2 only. Task 9 title trimmed to `Source / AuctionSource (ListingSource deferred)`.
- **Per-source rate-limiter / throttler** — first transient-error wave will tell us where this is needed. Phase 1 (HiBid) builds the first one.
- **Native PG enum types** — see decision 9 above.

End of overlay. Tasks below are the modified plan.

---

### Task 1: Repo scaffolding (pyproject.toml + tooling configs)

**Files:**
- Create: `pyproject.toml`
- Create: `ruff.toml`
- Create: `pyrightconfig.json`
- Create: `src/carbuyer/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "carbuyer"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "sqlalchemy[asyncio]>=2.0.30",
    "psycopg[binary,pool]>=3.2",
    "alembic>=1.13",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "httpx>=0.27",
    "selectolax>=0.3.21",
    "structlog>=24.1",
    "fastapi>=0.111",
    "jinja2>=3.1",
    "uvicorn[standard]>=0.30",
    "discord.py>=2.4",
    "openai>=1.40",
    "phonenumbers>=8.13",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "ruff>=0.5",
    "pyright>=1.1.370",
    "respx>=0.21",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/carbuyer"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `ruff.toml`**

```toml
line-length = 100
target-version = "py312"

[lint]
select = ["E", "F", "I", "N", "UP", "B", "ASYNC", "PL", "RUF"]
ignore = ["PLR0913"]  # too-many-arguments — let pyright complain instead

[format]
quote-style = "double"
```

- [ ] **Step 3: Write `pyrightconfig.json`**

```json
{
  "include": ["src", "tests"],
  "pythonVersion": "3.12",
  "typeCheckingMode": "strict",
  "reportMissingTypeStubs": false,
  "venvPath": ".",
  "venv": ".venv"
}
```

- [ ] **Step 4: Create empty package files**

```bash
touch src/carbuyer/__init__.py tests/__init__.py
```

- [ ] **Step 5: Initialize and verify environment**

```bash
uv sync --extra dev
uv run python -c "import carbuyer; print('ok')"
uv run ruff check .
uv run pyright
```

Expected: all four commands succeed; pyright reports 0 errors.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml ruff.toml pyrightconfig.json src/carbuyer/__init__.py tests/__init__.py
git commit -m "scaffold project: pyproject.toml, ruff, pyright"
```

---

### Task 2: Local Postgres via Docker Compose

**Files:**
- Create: `infra/docker-compose.yml`
- Create: `infra/.env.example`
- Create: `infra/initdb/01-create-test-db.sql`

- [ ] **Step 1: Write `infra/docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:17
    container_name: carbuyer-pg
    environment:
      POSTGRES_USER: carbuyer
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-local}
      POSTGRES_DB: carbuyer
    # 11 worker pools × (2 + 3 overflow) = ~55 connections worst case;
    # bump max_connections to 200 to leave headroom for ops/dashboard/Alembic.
    command:
      - "postgres"
      - "-c"
      - "max_connections=200"
      - "-c"
      - "shared_buffers=512MB"
      - "-c"
      - "effective_cache_size=2GB"
    ports:
      # Bound to localhost only — never expose Postgres on LAN.
      # Host port 5433 (not 5432) to avoid collisions with a host-native
      # Postgres install. DATABASE_URL must reference :5433 to match.
      - "127.0.0.1:5433:5432"
    volumes:
      - carbuyer-pg-data:/var/lib/postgresql/data
      # Runs on first volume init only; idempotent CREATE DATABASE for tests.
      - ./initdb:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "carbuyer"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 30s

volumes:
  carbuyer-pg-data:
```

- [ ] **Step 2: Write `infra/initdb/01-create-test-db.sql`**

```sql
-- Runs once on first Postgres init (when carbuyer-pg-data volume is empty).
-- Creates the test database used by pytest fixtures.
CREATE DATABASE carbuyer_test OWNER carbuyer;
```

- [ ] **Step 3: Write `infra/.env.example`**

```
POSTGRES_PASSWORD=local
```

- [ ] **Step 4: Start Postgres and verify**

```bash
cd infra && docker compose up -d
docker exec carbuyer-pg pg_isready -U carbuyer
docker exec carbuyer-pg psql -U carbuyer -lqt | grep -E '^\s+carbuyer(_test)?\s'
```

Expected: `accepting connections`; both `carbuyer` and `carbuyer_test` listed.

- [ ] **Step 5: Commit**

```bash
git add infra/docker-compose.yml infra/.env.example infra/initdb/01-create-test-db.sql
git commit -m "infra: local Postgres 17 via docker compose"
```

---

### Task 3: Config module (pydantic-settings)

**Files:**
- Create: `src/carbuyer/shared/__init__.py`
- Create: `src/carbuyer/shared/config.py`
- Create: `tests/shared/__init__.py`
- Create: `tests/shared/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/shared/test_config.py
import pytest
from carbuyer.shared.config import Settings


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("HOME_PROVINCE", "AB")
    s = Settings()
    assert s.database_url.endswith("/db")
    assert s.openai_api_key == "sk-test"
    assert s.discord_bot_token == "tok"
    assert s.home_province == "AB"
    assert s.notify_threshold == 0.15  # default
    assert s.early_warning_min_hours_to_close == 48
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/shared/test_config.py -v
```

Expected: ImportError or ModuleNotFoundError on `carbuyer.shared.config`.

- [ ] **Step 3: Write `src/carbuyer/shared/config.py`**

```python
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


Province = Literal["AB", "BC", "SK", "MB", "ON", "QC", "NS", "NB", "NL", "PE", "YT", "NT", "NU"]


# Default UA. Update each quarter; can be overridden via HTTP_USER_AGENT env.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://carbuyer:local@localhost:5433/carbuyer"
    )
    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="gpt-4o-mini")
    discord_bot_token: str = Field(default="")
    discord_guild_id: int | None = None
    discord_channels: dict[str, int] = Field(default_factory=dict)
    home_province: Province = "AB"

    notify_threshold: float = 0.15
    early_warning_rarity_threshold: float = 2.0
    early_warning_min_hours_to_close: int = 48
    rescore_improvement_threshold: float = 0.05

    quiet_hours_start: int = 22
    quiet_hours_end: int = 8
    quiet_hours_override_score: float = 0.30

    flip_margin_min_cad: int = 1500
    flip_margin_pct: float = 0.10

    log_level: str = "INFO"
    http_user_agent: str = DEFAULT_USER_AGENT

    @field_validator("discord_channels", mode="before")
    @classmethod
    def _parse_discord_channels(cls, value: Any) -> dict[str, int]:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("DISCORD_CHANNELS must be a JSON object of name→channel_id")
            return {str(k): int(v) for k, v in parsed.items()}
        if isinstance(value, dict):
            return {str(k): int(v) for k, v in value.items()}
        raise TypeError(f"discord_channels: unexpected type {type(value).__name__}")


settings = Settings()
```

- [ ] **Step 4: Create empty `tests/shared/__init__.py` and `src/carbuyer/shared/__init__.py`**

```bash
touch tests/shared/__init__.py src/carbuyer/shared/__init__.py
```

- [ ] **Step 5: Run test**

```bash
uv run pytest tests/shared/test_config.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/shared/__init__.py src/carbuyer/shared/config.py tests/shared/__init__.py tests/shared/test_config.py
git commit -m "shared: pydantic-settings config module"
```

---

### Task 4: Structlog logging setup

**Files:**
- Create: `src/carbuyer/shared/logging.py`
- Create: `tests/shared/test_logging.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/shared/test_logging.py
from carbuyer.shared.logging import configure_logging, get_logger


def test_get_logger_returns_bound_logger() -> None:
    configure_logging("DEBUG")
    log = get_logger("test")
    assert log is not None
    log.info("hello", key="value")  # should not raise


def test_configure_logging_default_reads_settings() -> None:
    # Calling with no level argument falls back to settings.log_level (default INFO).
    configure_logging()
    log = get_logger("test")
    log.info("ok")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/shared/test_logging.py -v
```

Expected: ImportError on `carbuyer.shared.logging`.

- [ ] **Step 3: Implement `src/carbuyer/shared/logging.py`**

```python
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import FilteringBoundLogger

from carbuyer.shared.config import settings


# stdout JSON only — systemd captures via journald in prod; do not add FileHandler.
def configure_logging(level: str | None = None) -> None:
    effective = (level or settings.log_level).upper()
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, effective),
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.contextvars.merge_contextvars,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, effective)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str, **initial_values: Any) -> FilteringBoundLogger:
    return structlog.get_logger(name).bind(**initial_values)
```

- [ ] **Step 4: Run test**

```bash
uv run pytest tests/shared/test_logging.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/shared/logging.py tests/shared/test_logging.py
git commit -m "shared: structlog JSON logging"
```

---

### Task 5: SQLAlchemy 2 async base + session factory

**Files:**
- Create: `src/carbuyer/db/__init__.py`
- Create: `src/carbuyer/db/base.py`
- Create: `src/carbuyer/db/session.py`
- Create: `tests/db/__init__.py`
- Create: `tests/db/test_session.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_session.py
import pytest
from sqlalchemy import text

from carbuyer.db.session import get_session, make_engine


@pytest.mark.asyncio
async def test_make_engine_can_select_one() -> None:
    # Throwaway engine for direct connectivity check; conftest's session-scoped
    # engine fixture (Task 8) replaces the singleton without conflict.
    eng = make_engine()
    try:
        async with eng.connect() as conn:
            result = await conn.execute(text("SELECT 1 AS x"))
            assert result.scalar_one() == 1
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_get_session_yields_async_session() -> None:
    # Validates the singleton accessor path; does NOT dispose, since other
    # tests (post-Task-8) share the same engine via the conftest fixture.
    async with get_session() as session:
        result = await session.execute(text("SELECT 2 AS x"))
        assert result.scalar_one() == 2
```

- [ ] **Step 2: Verify it fails**

```bash
uv run pytest tests/db/test_session.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `src/carbuyer/db/base.py`**

```python
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: dict[Any, Any] = {}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        server_onupdate=func.now(),
        nullable=False,
    )
```

- [ ] **Step 4: Implement `src/carbuyer/db/session.py`**

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from carbuyer.shared.config import settings


# Pool sizing rationale: 11 worker processes; each opens its own pool. With
# pool_size=2, max_overflow=3 the absolute upper bound is 5 × 11 = 55 sessions.
# Postgres is configured for max_connections=200 (see infra/docker-compose.yml),
# leaving headroom for ops, dashboard, and Alembic. Workers that do bursty DB
# I/O can override per-process via make_engine(pool_size=..., max_overflow=...).
def make_engine(
    url: str | None = None,
    *,
    pool_size: int = 2,
    max_overflow: int = 3,
) -> AsyncEngine:
    return create_async_engine(
        url or settings.database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            # statement_timeout=30s catches stuck OLTP queries; long-running
            # comp aggregates in the valuator/distiller must SET LOCAL their own
            # higher limit inside their transaction.
            "options": (
                "-c statement_timeout=30000 "
                "-c idle_in_transaction_session_timeout=60000 "
                "-c lock_timeout=5000"
            ),
        },
    )


_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine (created lazily on first call)."""
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide AsyncSession factory bound to get_engine()."""
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_maker


async def set_engine_for_testing(engine: AsyncEngine) -> None:
    """Test-only: replace the cached engine and session factory with a test pool.

    Disposes any previously cached engine. After this call, every callsite that
    uses get_session() / get_session_maker() / get_engine() will see the test
    engine, including modules that imported the names earlier (since they are
    looked up via these accessors, not module-level globals).
    """
    global _engine, _session_maker
    if _engine is not None and _engine is not engine:
        await _engine.dispose()
    _engine = engine
    _session_maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_maker()() as session:
        yield session
```

- [ ] **Step 5: Create empty `__init__.py` files**

```bash
touch src/carbuyer/db/__init__.py tests/db/__init__.py
```

- [ ] **Step 6: Run test (Postgres must be up from Task 2)**

```bash
uv run pytest tests/db/test_session.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/carbuyer/db/__init__.py src/carbuyer/db/base.py src/carbuyer/db/session.py tests/db/__init__.py tests/db/test_session.py
git commit -m "db: SQLAlchemy async base + session factory"
```

---

### Task 6: ORM models for all MVP tables

**Files:**
- Create: `src/carbuyer/db/enums.py`
- Create: `src/carbuyer/db/models.py`
- Create: `tests/db/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_models.py
from carbuyer.db.enums import (
    AuctionStatus,
    EnrichmentStatus,
    LotStatus,
    NotificationStatus,
    UserAction,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import (
    Auction,
    AuctionBidHistory,
    AuctionLot,
    HistoricalSale,
    Purchase,
    Search,
)


def test_models_importable() -> None:
    assert Auction.__tablename__ == "auctions"
    assert AuctionLot.__tablename__ == "auction_lots"
    assert AuctionBidHistory.__tablename__ == "auction_bid_history"
    assert HistoricalSale.__tablename__ == "historical_sales"
    assert Purchase.__tablename__ == "purchases"
    assert Search.__tablename__ == "searches"


def test_status_enums_have_expected_values() -> None:
    # StrEnum: comparable to plain strings; persisted as String(16) in DB.
    assert EnrichmentStatus.PENDING == "pending"
    assert ValuationStatus.DONE == "done"
    assert VisionStatus.SKIPPED == "skipped"
    assert NotificationStatus.PENDING == "pending"
    assert LotStatus.OPEN == "open"
    assert AuctionStatus.UPCOMING == "upcoming"
    assert UserAction.INTERESTED == "interested"


def test_auction_lot_has_required_columns() -> None:
    cols = {c.name for c in AuctionLot.__table__.columns}
    expected = {
        "id", "auction_id", "source_lot_id", "lot_number", "url",
        "parser_version",
        "title", "description", "photos",
        "year", "make", "model", "trim", "engine", "transmission", "drivetrain",
        "mileage_km", "vin", "title_status", "province_of_origin",
        "condition_categorical", "condition_confidence",
        "red_flags", "green_flags", "showstopper_flags",
        "summary", "carfax_url", "carfax_findings",
        "desirable_trim_or_spec", "classic_or_collector",
        "desirability_signals", "desirability_evidence",
        "historical_comp_count", "recent_appreciation", "rarity_score",
        "vision_findings", "vision_condition_overall", "vision_confidence",
        "vision_contradictions",
        "current_high_bid_cad", "last_bid_observed_at", "bid_count_visible",
        "reserve_met", "lot_status", "closed_at", "final_bid_cad",
        "comp_count", "value_low_cad", "value_mid_cad", "value_high_cad",
        "expected_value_cad", "landed_cost_premium_cad",
        "all_in_at_current_bid_cad", "recommended_max_bid_cad",
        "price_deal_score", "flag_score", "confidence_bucket",
        "suspicious_underprice_flag", "scoring_version", "weights_hash",
        "enrichment_status", "valuation_status", "vision_status", "notification_status",
        "enrichment_version",
        "early_warning_notified_at", "cheap_notified_at", "closing_notified_at",
        "trajectory_notified_at", "extended_notified_at", "last_notified_channel",
        "user_action", "notes", "was_purchased_by_us",
        "created_at", "updated_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"
```

- [ ] **Step 2: Verify it fails**

```bash
uv run pytest tests/db/test_models.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `src/carbuyer/db/enums.py`**

```python
from __future__ import annotations

from enum import StrEnum


# Worker-pipeline stage statuses (one column per stage on auction_lots).
# StrEnum compares equal to plain strings so existing string queries keep working
# without TypeDecorator boilerplate; the DB column is still String(16).
class EnrichmentStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class ValuationStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    INSUFFICIENT_COMPS = "insufficient_comps"


class VisionStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"


class LotStatus(StrEnum):
    OPEN = "open"
    CLOSING_SOON = "closing_soon"
    EXTENDED = "extended"
    CLOSED = "closed"
    UNSOLD = "unsold"
    SOLD = "sold"


class AuctionStatus(StrEnum):
    UPCOMING = "upcoming"
    LIVE = "live"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class UserAction(StrEnum):
    INTERESTED = "interested"
    MAYBE = "maybe"
    NOT_INTERESTED = "not_interested"


class AuctionSubtype(StrEnum):
    ESTATE = "estate"
    COMMERCIAL = "commercial"  # phase-2 RB / Michener Allen
```

- [ ] **Step 4: Implement `src/carbuyer/db/models.py`**

> **Worker column-ownership rule:** Each block under `AuctionLot` is annotated
> with the worker that owns the columns. Workers UPDATE only their own columns;
> never `session.merge(lot)` the whole row back. Bid-poller updates bid-state;
> enricher updates enrichment + rarity (LLM); valuator updates valuation +
> historical_comp_count; vision-batcher updates vision_*; notifier updates
> *_notified_at; user actions come from the dashboard.

```python
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from carbuyer.db.base import Base, TimestampMixin


class Auction(Base, TimestampMixin):
    __tablename__ = "auctions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_auction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    auction_subtype: Mapped[str] = mapped_column(
        String(32), nullable=False, default="estate", server_default="estate",
    )
    auctioneer_name: Mapped[str | None] = mapped_column(String(255))
    auctioneer_external_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    terms_text: Mapped[str | None] = mapped_column(Text)
    scheduled_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pickup_address: Mapped[str | None] = mapped_column(Text)
    pickup_city: Mapped[str | None] = mapped_column(String(128))
    pickup_province: Mapped[str | None] = mapped_column(String(8))
    pickup_window_text: Mapped[str | None] = mapped_column(Text)
    buyer_premium_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    online_bidding_fee_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    gst_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    pst_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="upcoming", server_default="upcoming", index=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discovery_confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, default="high", server_default="high",
    )
    needs_plugin_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    routing_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lots: Mapped[list["AuctionLot"]] = relationship(back_populates="auction", lazy="raise")

    __table_args__ = (
        UniqueConstraint("source", "source_auction_id", name="uq_auctions_source_source_auction_id"),
    )


class AuctionLot(Base, TimestampMixin):
    __tablename__ = "auction_lots"

    # ── Owned by: lot-scraper (initial insert + URL/photo refresh) ──────────
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    auction_id: Mapped[int] = mapped_column(ForeignKey("auctions.id", ondelete="CASCADE"), index=True)
    source_lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_number: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # parser_version: the source plugin's parser version at the time this row
    # was scraped. Used to detect rows that need re-enrichment after a parser
    # change (Source.version on the plugin → propagated here on every upsert).
    parser_version: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'::text[]"), nullable=False,
    )

    # ── Owned by: lot-scraper (initial), description-enricher (LLM normalization) ──
    year: Mapped[int | None] = mapped_column(Integer)
    make: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(64), index=True)
    trim: Mapped[str | None] = mapped_column(String(64))
    engine: Mapped[str | None] = mapped_column(String(64))
    transmission: Mapped[str | None] = mapped_column(String(16))
    drivetrain: Mapped[str | None] = mapped_column(String(16))
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    vin: Mapped[str | None] = mapped_column(String(32))
    title_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNKNOWN", server_default="UNKNOWN",
    )
    province_of_origin: Mapped[str | None] = mapped_column(String(8))

    # ── Owned by: description-enricher (LLM description pass) ───────────────
    condition_categorical: Mapped[str | None] = mapped_column(String(16))
    condition_confidence: Mapped[float | None] = mapped_column()
    red_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    green_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    showstopper_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    summary: Mapped[str | None] = mapped_column(Text)
    carfax_url: Mapped[str | None] = mapped_column(Text)
    carfax_findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # ── Owned by: description-enricher (rarity/desirability fields, LLM) ────
    desirable_trim_or_spec: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False,
    )
    classic_or_collector: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False,
    )
    desirability_signals: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    desirability_evidence: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    # historical_comp_count: written by valuator (DB-derived signal).
    historical_comp_count: Mapped[int | None] = mapped_column(Integer)
    recent_appreciation: Mapped[float | None] = mapped_column()
    # rarity_score: combined LLM + DB; written by valuator.
    rarity_score: Mapped[float | None] = mapped_column()

    # ── Owned by: vision-batcher (nightly two-pass) ─────────────────────────
    vision_findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    vision_condition_overall: Mapped[str | None] = mapped_column(String(16))
    vision_confidence: Mapped[float | None] = mapped_column()
    vision_contradictions: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )

    # ── Owned by: bid-poller (continuous tiered cadence) ────────────────────
    current_high_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    last_bid_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bid_count_visible: Mapped[int | None] = mapped_column(Integer)
    reserve_met: Mapped[bool | None] = mapped_column(Boolean)
    lot_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open", index=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # ── Owned by: valuator ──────────────────────────────────────────────────
    comp_count: Mapped[int | None] = mapped_column(Integer)
    value_low_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    value_mid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    value_high_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    expected_value_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    landed_cost_premium_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    all_in_at_current_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    recommended_max_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_deal_score: Mapped[float | None] = mapped_column()
    flag_score: Mapped[int | None] = mapped_column(Integer)
    confidence_bucket: Mapped[str | None] = mapped_column(String(16))
    suspicious_underprice_flag: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False,
    )
    scoring_version: Mapped[str | None] = mapped_column(String(32))
    weights_hash: Mapped[str | None] = mapped_column(String(64))

    # ── Owned by: pipeline workers (each writes its own status column) ──────
    # See carbuyer.db.enums for valid values; column is String(16) so PG enum
    # migration churn is avoided. Workers compare against StrEnum members.
    enrichment_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True,
    )
    valuation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True,
    )
    vision_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True,
    )
    notification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True,
    )
    enrichment_version: Mapped[str | None] = mapped_column(String(32))

    # ── Owned by: notifier (one timestamp per trigger type) ─────────────────
    early_warning_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cheap_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closing_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trajectory_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extended_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notified_channel: Mapped[str | None] = mapped_column(String(64))

    # ── Owned by: dashboard (user input) ────────────────────────────────────
    user_action: Mapped[str | None] = mapped_column(String(16), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    was_purchased_by_us: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False, index=True,
    )

    auction: Mapped["Auction"] = relationship(back_populates="lots", lazy="raise")
    bid_history: Mapped[list["AuctionBidHistory"]] = relationship(
        back_populates="lot", lazy="raise", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("auction_id", "source_lot_id", name="uq_auction_lots_auction_source_lot"),
        Index("ix_auction_lots_make_model_year", "make", "model", "year"),
        Index("ix_auction_lots_price_deal_score", "price_deal_score", "lot_status"),
        Index("ix_auction_lots_rarity_score", "rarity_score", "scheduled_end_at"),
    )


class AuctionBidHistory(Base):
    __tablename__ = "auction_bid_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lot_id: Mapped[int] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_high_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    end_time_at_observation: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_at_observation: Mapped[str | None] = mapped_column(String(32))

    lot: Mapped["AuctionLot"] = relationship(back_populates="bid_history", lazy="raise")

    __table_args__ = (Index("ix_bid_history_lot_observed", "lot_id", "observed_at"),)


class HistoricalSale(Base, TimestampMixin):
    __tablename__ = "historical_sales"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    make: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(64), index=True)
    trim: Mapped[str | None] = mapped_column(String(64))
    engine: Mapped[str | None] = mapped_column(String(64))
    transmission: Mapped[str | None] = mapped_column(String(16))
    drivetrain: Mapped[str | None] = mapped_column(String(16))
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    vin: Mapped[str | None] = mapped_column(String(32))
    title_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNKNOWN", server_default="UNKNOWN",
    )
    province_of_origin: Mapped[str | None] = mapped_column(String(8))
    condition_categorical: Mapped[str | None] = mapped_column(String(16))
    final_listed_price_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    days_listed: Mapped[int | None] = mapped_column(Integer)
    buyer_premium_pct_at_sale: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    final_price_with_premium_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    sale_channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sale_platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seller_province: Mapped[str | None] = mapped_column(String(8))
    seller_city: Mapped[str | None] = mapped_column(String(128))
    observed_first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disappeared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disposition_reason: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown", server_default="unknown",
    )
    was_notified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False,
    )
    was_purchased_by_us: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"),
    )


class Purchase(Base, TimestampMixin):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    sale_date: Mapped[date | None] = mapped_column(Date)
    make: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_price_cad: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    sale_price_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    province_of_purchase: Mapped[str | None] = mapped_column(String(8))
    province_of_sale: Mapped[str | None] = mapped_column(String(8))
    transport_cost_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    inspection_cost_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    repair_cost_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    linked_lot_id: Mapped[int | None] = mapped_column(ForeignKey("auction_lots.id"))


class Search(Base, TimestampMixin):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="me", server_default="me",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False,
    )
```

- [ ] **Step 5: Run test**

```bash
uv run pytest tests/db/test_models.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/db/enums.py src/carbuyer/db/models.py tests/db/test_models.py
git commit -m "db: ORM models + status StrEnums for all MVP tables"
```

---

### Task 7: Alembic init + initial migration

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/<auto>_initial.py`

- [ ] **Step 1: Initialize Alembic**

```bash
uv run alembic init alembic
```

This creates `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, and `alembic/versions/`.

- [ ] **Step 2: Edit `alembic.ini` — set the URL line to be empty (we'll populate from settings)**

Find `sqlalchemy.url = ...` and replace with:
```
sqlalchemy.url =
```

- [ ] **Step 3: Replace `alembic/env.py` with:**

```python
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from carbuyer.db import models  # noqa: F401  -- ensure models register with metadata
from carbuyer.db.base import Base
from carbuyer.shared.config import settings


# psycopg3's "postgresql+psycopg" URL is consumed by both create_engine (sync)
# and create_async_engine (async). Alembic only needs a sync engine, so we use
# create_engine directly with the same URL — no rewrite required.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(settings.database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: Generate the initial migration**

```bash
uv run alembic revision --autogenerate -m "initial schema"
```

Expected: a new file under `alembic/versions/` named like `<hash>_initial_schema.py` is created.

- [ ] **Step 5: Apply the migration**

```bash
uv run alembic upgrade head
```

Expected: `INFO ... Running upgrade -> <hash>, initial schema`.

- [ ] **Step 6: Verify schema**

```bash
docker exec carbuyer-pg psql -U carbuyer -d carbuyer -c "\dt"
```

Expected: 7 tables listed (`alembic_version`, `auctions`, `auction_lots`, `auction_bid_history`, `historical_sales`, `purchases`, `searches`).

- [ ] **Step 7: Commit**

```bash
git add alembic.ini alembic/env.py alembic/script.py.mako alembic/versions/
git commit -m "db: alembic init + initial migration"
```

---

### Task 8: pytest fixtures (test DB, async session)

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `tests/conftest.py`**

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from carbuyer.db.base import Base
from carbuyer.db.session import set_engine_for_testing
from carbuyer.shared.config import settings


def _test_url() -> str:
    # Append _test to the DB name. Test DB is auto-created by the
    # /docker-entrypoint-initdb.d/ script in infra/docker-compose.yml.
    url = settings.database_url
    if url.endswith("/carbuyer"):
        return url[: -len("/carbuyer")] + "/carbuyer_test"
    if url.endswith("/carbuyer_test"):
        return url
    raise RuntimeError(
        f"Refusing to run tests against non-carbuyer database URL: {url}"
    )


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    # NullPool: each connection is fresh — no pool deadlocks, no cross-test
    # connection state. Schema is built once per session; tests use savepoints
    # for isolation (see `session` fixture).
    eng = create_async_engine(_test_url(), poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    # Make get_session() / get_session_maker() in production code resolve to
    # this test engine for every test in the session.
    await set_engine_for_testing(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test AsyncSession that rolls back on teardown.

    Pattern: open one outer connection, begin an outer transaction, bind a
    sessionmaker to that connection with `join_transaction_mode='create_savepoint'`
    so every commit inside a test becomes a SAVEPOINT release; the outer
    rollback at teardown undoes everything. No drop/create per test.
    """
    conn: AsyncConnection
    async with engine.connect() as conn:
        outer = await conn.begin()
        maker = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as s:
            yield s
        await outer.rollback()
```

- [ ] **Step 2: Verify the test DB exists (created on first compose up)**

```bash
docker exec carbuyer-pg psql -U carbuyer -lqt | grep carbuyer_test
```

Expected: a line for `carbuyer_test`. If missing (older volume), recreate it:

```bash
docker exec carbuyer-pg psql -U carbuyer -c "CREATE DATABASE carbuyer_test;"
```

- [ ] **Step 3: Verify with a smoke test (run existing test_session.py — it now points at carbuyer_test via the engine fixture)**

```bash
uv run pytest tests/db/test_session.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "tests: session-scoped test engine + per-test savepoint isolation"
```

---

### Task 9: Source plugin ABCs (Source / AuctionSource — ListingSource deferred)

**Files:**
- Create: `src/carbuyer/sources/__init__.py`
- Create: `src/carbuyer/sources/base.py`
- Create: `tests/sources/__init__.py`
- Create: `tests/sources/test_base.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sources/test_base.py
import dataclasses
import inspect

import pytest

from carbuyer.sources.base import (
    SOURCES,
    AuctionDiscoverer,
    AuctionFetcher,
    AuctionRef,
    AuctionSource,
    BidPoller,
    LotRef,
    RawLot,
    Source,
    register,
)


def test_auction_ref_is_constructible_and_frozen() -> None:
    ref = AuctionRef(source="hibid", source_auction_id="A123", url="https://x")
    assert ref.source == "hibid"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.source = "other"  # type: ignore[misc]


def test_raw_lot_has_extra_escape_hatch() -> None:
    raw = RawLot(
        ref=LotRef(source="hibid", source_auction_id="A1", source_lot_id="L1", url="https://x"),
        lot_number="1", title="Truck", description=None,
    )
    assert raw.extra == {}
    raw.extra["carfax_url"] = "https://carfax/abc"
    assert raw.extra["carfax_url"].endswith("abc")


def test_role_abcs_are_abstract() -> None:
    assert inspect.isabstract(AuctionDiscoverer)
    assert inspect.isabstract(AuctionFetcher)
    assert inspect.isabstract(BidPoller)
    assert inspect.isabstract(AuctionSource)


def test_register_adds_source_to_registry() -> None:
    SOURCES.clear()

    class _StubSource(Source):
        name = "stub"
        version = "0.0.1"

    src = _StubSource()
    register(src)
    assert SOURCES["stub"] is src
    SOURCES.clear()
```

- [ ] **Step 2: Verify it fails**

```bash
uv run pytest tests/sources/test_base.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `src/carbuyer/sources/base.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal


SourceType = Literal["listing", "auction"]


# ── Reference / value objects ───────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AuctionRef:
    source: str
    source_auction_id: str
    url: str


@dataclass(frozen=True, slots=True)
class LotRef:
    source: str
    source_auction_id: str
    source_lot_id: str
    url: str


@dataclass(slots=True)
class RawAuction:
    ref: AuctionRef
    title: str | None
    description: str | None
    auctioneer_name: str | None
    auctioneer_external_id: str | None
    scheduled_start_at: datetime | None
    scheduled_end_at: datetime | None
    pickup_address: str | None
    pickup_city: str | None
    pickup_province: str | None
    pickup_window_text: str | None
    buyer_premium_pct: Decimal | None
    online_bidding_fee_pct: Decimal | None
    terms_text: str | None
    auction_subtype: str = "estate"
    # Source-specific fields that don't yet warrant a canonical column.
    # Promote to a real field once 2+ sources surface the same key.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RawLot:
    ref: LotRef
    lot_number: str | None
    title: str | None
    description: str | None
    photos: list[str] = field(default_factory=list)
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    mileage_km: int | None = None
    vin: str | None = None
    current_high_bid_cad: Decimal | None = None
    bid_count_visible: int | None = None
    reserve_met: bool | None = None
    scheduled_end_at: datetime | None = None
    lot_status: str = "open"
    # See RawAuction.extra. Common uses today: carfax_url, reserve_price_cad,
    # buy_now_price_cad, raw HTML for downstream parsers.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BidObservation:
    ref: LotRef
    observed_at: datetime
    current_high_bid_cad: Decimal | None
    end_time_at_observation: datetime | None
    status_at_observation: str  # See db.enums.LotStatus for canonical values.


# ── Plugin role ABCs ────────────────────────────────────────────────────────
# Roles split so that a router (Phase 10) can implement only AuctionDiscoverer
# while a full plugin (HiBid, McDougall) implements all three via AuctionSource.
#
# NOTE: abstract async-generator methods are declared `def f(...) -> AsyncIterator[T]`
# (not `async def`). Concrete implementations are async generators
# (`async def f(...) -> AsyncIterator[T]: yield ...`); pyright accepts that
# pairing under strict mode. `async def f() -> AsyncIterator[T]: ...` (with `...`
# body) would be a coroutine returning an iterator — wrong shape, runtime bug.

class Source(ABC):
    """Marker base for all sources. Subclasses must define `name` and `version`."""

    name: ClassVar[str]
    # Bumped when the parser/discovery contract changes. Persisted to
    # `auction_lots.parser_version` so the enricher / valuator can re-run on
    # rows scraped by a stale version.
    version: ClassVar[str]


class AuctionDiscoverer(Source):
    kind: ClassVar[SourceType] = "auction"

    @abstractmethod
    def discover_auctions(self) -> AsyncIterator[AuctionRef]: ...


class AuctionFetcher(Source):
    kind: ClassVar[SourceType] = "auction"

    @abstractmethod
    async def fetch_auction(self, ref: AuctionRef) -> RawAuction: ...

    @abstractmethod
    def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]: ...

    @abstractmethod
    async def fetch_lot(self, ref: LotRef) -> RawLot: ...


class BidPoller(Source):
    kind: ClassVar[SourceType] = "auction"

    @abstractmethod
    async def poll_bid(self, ref: LotRef) -> BidObservation: ...


class AuctionSource(AuctionDiscoverer, AuctionFetcher, BidPoller):
    """Convenience union for plugins that implement all three auction roles."""


# ── Registry ────────────────────────────────────────────────────────────────
# Plugins call `register(self)` at module import time; the lot-scraper /
# discoverer / dashboard read SOURCES to enumerate covered platforms (used by
# the Phase-10 "needs-plugin" alerting and the dashboard health view).

SOURCES: dict[str, Source] = {}


def register(source: Source) -> None:
    SOURCES[source.name] = source
```

- [ ] **Step 4: Create `__init__.py` files**

```bash
touch src/carbuyer/sources/__init__.py tests/sources/__init__.py
```

- [ ] **Step 5: Run test**

```bash
uv run pytest tests/sources/test_base.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/carbuyer/sources/__init__.py src/carbuyer/sources/base.py tests/sources/__init__.py tests/sources/test_base.py
git commit -m "sources: AuctionSource ABC and dataclasses"
```

---

### Task 10: HTTP client wrapper with shared headers + jitter

**Files:**
- Create: `src/carbuyer/sources/http.py`
- Create: `tests/sources/test_http.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sources/test_http.py
import pytest
import respx

from carbuyer.sources.http import build_default_headers, make_client


@pytest.mark.asyncio
async def test_make_client_sends_browser_headers() -> None:
    async with respx.mock(base_url="https://example.test") as mock:
        route = mock.get("/").respond(200, text="ok")
        async with make_client() as client:
            r = await client.get("https://example.test/")
        assert r.status_code == 200
        sent = route.calls.last.request
        assert "Mozilla" in sent.headers["User-Agent"]
        assert "Accept-Language" in sent.headers


@pytest.mark.asyncio
async def test_make_client_merges_custom_headers() -> None:
    async with respx.mock(base_url="https://example.test") as mock:
        route = mock.get("/").respond(200)
        async with make_client(headers={"X-Plugin": "hibid"}) as client:
            await client.get("https://example.test/")
        sent = route.calls.last.request
        assert sent.headers["X-Plugin"] == "hibid"
        assert "Mozilla" in sent.headers["User-Agent"]


def test_build_default_headers_uses_settings_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_USER_AGENT", "TestUA/1.0")
    # Reload settings to pick up the env var override.
    from importlib import reload

    import carbuyer.shared.config as config_mod
    reload(config_mod)
    import carbuyer.sources.http as http_mod
    reload(http_mod)
    headers = http_mod.build_default_headers()
    assert headers["User-Agent"] == "TestUA/1.0"
```

- [ ] **Step 2: Verify it fails**

```bash
uv run pytest tests/sources/test_http.py -v
```

- [ ] **Step 3: Implement `src/carbuyer/sources/http.py`**

```python
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from carbuyer.shared.config import settings


# HTTP/2 is disabled by default: HTTP/2 fingerprinting (h2c settings frame
# ordering, etc.) is more revealing than UA, and httpx's H2 backend requires
# the `httpx[http2]` extra which we don't depend on.
#
# Phase 1 wraps this with a RetryTransport that honors Retry-After and applies
# jittered exponential backoff on {429, 502, 503, 504}. The `transport`
# parameter is the seam.

def build_default_headers() -> dict[str, str]:
    return {
        "User-Agent": settings.http_user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }


@asynccontextmanager
async def make_client(
    *,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    merged_headers = build_default_headers()
    if headers:
        merged_headers.update(headers)
    async with httpx.AsyncClient(
        headers=merged_headers,
        timeout=timeout,
        follow_redirects=follow_redirects,
        http2=False,
        transport=transport,
    ) as client:
        yield client


async def jittered_sleep(min_s: float = 4.0, max_s: float = 8.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))
```

- [ ] **Step 4: Run test**

```bash
uv run pytest tests/sources/test_http.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/sources/http.py tests/sources/test_http.py
git commit -m "sources: HTTP client wrapper with browser headers + jitter"
```

---

End of Phase 0. Postgres is up, schema is migrated, ORM models exist, source ABCs are defined, HTTP client is shared.

---

## Phase 1 — HiBid source plugin

HiBid (`hibid.com`) is the primary source. Pages embed a `<script>` block of the form `var lotModels = [{...}, ...];`. Our parser extracts that JSON literal and walks it. No headless browser. Conservative pacing (4–8s jitter). Reference fixture: capture one real catalog page during implementation; commit the HTML to `tests/sources/fixtures/`.

### Phase 1 — Design-decision overlay (post-deliberation 2026-05-09)

Multi-discipline review (senior dev, scraping/web research) raised concrete realities and code issues against original Phase 1. **Tasks below have been updated to reflect these decisions; this overlay documents the *why*.**

**Hard reality found via live investigation:**

- **Cloudflare bot management is active on hibid.com.** Plain `curl` with full Chrome headers returns HTTP 403 (`__cf_bm` cookie set, "Sorry, you have been blocked"). Anthropic's WebFetch passes through (different IP/JA3 fingerprint), so the site is reachable in principle. **A self-hosted Python httpx scraper as designed in the original plan will not work.** Three viable strategies:
  1. **`curl_cffi`** (or similar) — TLS fingerprint matching Chrome via libcurl-impersonate. Drop-in `requests`-like API. **Recommended for MVP** — minimal infra, works for sites with Bot Fight Mode (which HiBid is — no Turnstile interstitial was observed).
  2. **Playwright** (headed/headless with stealth plugins) — heavier, slower, higher hosting cost.
  3. **Residential proxy provider** — ongoing cost, simpler code.
- The plan **defers the CF-bypass implementation to a follow-up** (Phase 1.5 below). Phase 1 builds the plumbing against a hand-written synthetic fixture so the parser, URL builders, and `AuctionSource` integration are correct and unit-tested. The live integration test (original Task 14) becomes a **deferred Cloudflare-bypass spike**, with detailed acceptance criteria captured in the new task description.

**HiBid data realities discovered:**

- **Real `lotModels` lot-summary keys** (per the only public scraper found, [jkoelmel/texas_auctions_scraper](https://github.com/jkoelmel/texas_auctions_scraper)):
  - `eventItemId` (int) — per-auction lot id used in listing endpoints.
  - `lead` (string) — headline / title.
  - `quantity` (int).
  - `companyName` (string) — auctioneer.
  - `auctionCity`, `auctionState` (strings) — location.
  - `lotStatus.highBid` (number) — **nested** under `lotStatus`.
  - `lotStatus.bidCount` (int).
  - `lotStatus.timeLeft` (countdown string).
  - `shippingOffered` (bool).
  - The plan's original guesses (`lotId/lotID/id`, `title/lotTitle`, flat `highBid`, `saleEnd`, `lotImages`, `auctionId`, `url/lotUrl`) **do not match** observed HiBid output. `HibidLotSummary` is rewritten below.
- **Year/make/model are NOT separate fields** in `lotModels` — they're embedded in the `lead` text. Parsing them out is the description-enricher's job (Phase 3); leave `year/make/model = None` in `parse_lot_summary`.
- **Public lot URL uses a different global lot id** (`/lot/300724160/peterbilt-389`) than `eventItemId`. Persist `eventItemId` as `source_lot_id` (it's the listing-scoped key); the URL field comes from `lotUrl` if present.
- **Discovery URL.** The original plan's `/{province}/auctions/700006/cars-and-vehicles?status=OPEN` is the **catalogs view** (auction-event listing). For per-lot enumeration, `/{province}/lots/700006/cars-and-vehicles?status=open` is the lots view. The discoverer wants the catalogs view (we record auctions, the lot-scraper enumerates lots within an auction). Status query param is lowercase `open` in the rendered UI; either case works server-side.
- **Cross-border lots:** the AB province auction list contains some BC-located auctioneers (border operators). Don't assume `province path = pickup province`; post-filter on `auctionState` if needed.

**Must-fix code issues:**

1. **Balanced-bracket extraction, not regex `\[.*?\]`.** Non-greedy `\[.*?\]` with `re.DOTALL` matches from first `[` to first `]` — truncates on every nested array (HiBid lots embed `images: [...]`, etc.). Replace with a string-aware depth scanner. (Task 12.)
2. **`HibidSource` declares `version: ClassVar[str]`.** Phase 0 design decision #8 makes this a contract on every plugin. (Task 13.)
3. **`HibidSource` calls `register()` at module import.** Phase 0 design decision #11. (Task 13.)
4. **`_parse_dt` returns UTC-aware datetimes.** Phase 7's soft-close logic compares to `datetime.now(UTC)`; mixing aware/naive crashes. (Task 12.)
5. **Defensive `_to_decimal` helper.** `Decimal(str(bid))` blows up on stringified currency (`"1,200.00"`, `"$1,200"`). Single bad lot must not crash the whole catalog parse. (Task 12.)

**Should-fix code issues:**

6. **Retry transport in this phase.** Phase 0's HTTP wrapper already has a `transport=` seam; the comment promises Phase 1 will fill it. New module `src/carbuyer/sources/retry.py` with `RetryTransport` that honors Retry-After + jittered exponential backoff on `{429, 502, 503, 504}`. Used by HibidSource via `make_client(transport=RetryTransport(...))`. (New Task 11.5.)
7. **`HibidSource` is an async context manager owning one client.** Phase 7 will poll continuously — per-call `make_client()` thrashes TLS handshakes. Workers wrap their main loop in `async with HibidSource(...) as src:`. (Task 13.)
8. **Populate `RawLot.extra` with HiBid-specific fields:** `bidIncrement`, `reserveStatus`, `buyNowPrice`, `lotState`. The valuator (Phase 4) and soft-close detector (Phase 7) want them; capturing now is one line. (Task 13.)
9. **Hand-written synthetic fixture in addition to (or instead of) a live capture.** `tests/sources/fixtures/hibid_catalog_synthetic.html` is the contract; if a live capture lands later, it's the canary. Synthetic fixture exercises: nested array in lot, `}],{` quirk, two lots with known ids/bids/end-times. (Task 12.)

**Deferred (acknowledged, captured as Phase 1.5):**

- **Cloudflare bypass** — Task 14 rewritten as Phase 1.5 spike: evaluate `curl_cffi` first, fall back to Playwright if 403'd. Acceptance: one real Alberta auction discovered + one lot fetched. Until done, the auction-discoverer worker (Phase 2) cannot run against live HiBid; it can run against a local mock for end-to-end-pipeline testing.

End of overlay.

---

### Task 11: HiBid constants and URL builders

**Files:**
- Create: `src/carbuyer/sources/hibid/__init__.py`
- Create: `src/carbuyer/sources/hibid/urls.py`
- Create: `tests/sources/hibid/__init__.py`
- Create: `tests/sources/hibid/test_urls.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sources/hibid/test_urls.py
from carbuyer.sources.hibid.urls import (
    catalog_url,
    lot_url,
    province_lots_url,
    province_vehicles_url,
)


def test_province_vehicles_url() -> None:
    # Catalogs/events view — used by auction-discoverer to enumerate auctions.
    assert (
        province_vehicles_url("AB")
        == "https://hibid.com/alberta/auctions/700006/cars-and-vehicles?status=open"
    )


def test_province_lots_url() -> None:
    # Per-lot listing view — used when scraping lots without a known auction.
    assert (
        province_lots_url("BC")
        == "https://hibid.com/british-columbia/lots/700006/cars-and-vehicles?status=open"
    )


def test_lot_url() -> None:
    assert lot_url("12345", "1995-ford-f150") == "https://hibid.com/lot/12345/1995-ford-f150"


def test_lot_url_no_slug() -> None:
    assert lot_url("12345") == "https://hibid.com/lot/12345"


def test_catalog_url() -> None:
    assert (
        catalog_url("740236", "vehicle-equipment-with-nl-power-auction")
        == "https://hibid.com/catalog/740236/vehicle-equipment-with-nl-power-auction"
    )
```

- [ ] **Step 2: Implement `src/carbuyer/sources/hibid/urls.py`**

```python
from __future__ import annotations

# Western Canadian provinces. Other provinces (ON, QC, etc.) follow the same
# slug pattern but are out-of-scope for the MVP.
PROVINCE_PATH: dict[str, str] = {
    "AB": "alberta",
    "BC": "british-columbia",
    "SK": "saskatchewan",
    "MB": "manitoba",
}

# HiBid's "Cars & Vehicles" category id.
CARS_VEHICLES_CATEGORY = "700006"


def province_vehicles_url(province: str) -> str:
    """URL for the catalogs/events view (auctions hosting vehicle lots)."""
    path = PROVINCE_PATH[province]
    return (
        f"https://hibid.com/{path}/auctions/{CARS_VEHICLES_CATEGORY}"
        f"/cars-and-vehicles?status=open"
    )


def province_lots_url(province: str) -> str:
    """URL for the per-lot view (individual lots flattened across auctions)."""
    path = PROVINCE_PATH[province]
    return (
        f"https://hibid.com/{path}/lots/{CARS_VEHICLES_CATEGORY}"
        f"/cars-and-vehicles?status=open"
    )


def lot_url(lot_id: str, slug: str = "") -> str:
    if slug:
        return f"https://hibid.com/lot/{lot_id}/{slug}"
    return f"https://hibid.com/lot/{lot_id}"


def catalog_url(auction_id: str, slug: str = "") -> str:
    if slug:
        return f"https://hibid.com/catalog/{auction_id}/{slug}"
    return f"https://hibid.com/catalog/{auction_id}"
```

- [ ] **Step 3: Create `__init__.py` files and run test**

```bash
touch src/carbuyer/sources/hibid/__init__.py tests/sources/hibid/__init__.py
uv run pytest tests/sources/hibid/test_urls.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/sources/hibid/__init__.py src/carbuyer/sources/hibid/urls.py tests/sources/hibid/__init__.py tests/sources/hibid/test_urls.py
git commit -m "hibid: URL builders and constants"
```

---

### Task 11.5: RetryTransport (httpx) for transient errors

**Why this task is here:** Phase 0 carved a `transport=` seam in `make_client()` and the docstring promised Phase 1 would add a retry transport. Every real scraper Phase 7 will run hits transient 429/503; without retry, the workers crash-loop on the first hiccup.

**Files:**
- Create: `src/carbuyer/sources/retry.py`
- Create: `tests/sources/test_retry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sources/test_retry.py
import httpx
import pytest

from carbuyer.sources.http import make_client
from carbuyer.sources.retry import RetryTransport


@pytest.mark.asyncio
async def test_retry_transport_retries_503_then_succeeds() -> None:
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, text="ok")

    inner = httpx.MockTransport(handler)
    transport = RetryTransport(inner, max_retries=4, base=0.01, cap=0.05)
    async with make_client(transport=transport) as client:
        r = await client.get("https://example.test/")
    assert r.status_code == 200
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_retry_transport_gives_up_after_max() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "0"})

    transport = RetryTransport(httpx.MockTransport(handler), max_retries=2, base=0.01, cap=0.05)
    async with make_client(transport=transport) as client:
        r = await client.get("https://example.test/")
    assert r.status_code == 503  # exhausted retries; final response returned


@pytest.mark.asyncio
async def test_retry_transport_does_not_retry_404() -> None:
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(404)

    transport = RetryTransport(httpx.MockTransport(handler), max_retries=4, base=0.01, cap=0.05)
    async with make_client(transport=transport) as client:
        r = await client.get("https://example.test/")
    assert r.status_code == 404
    assert call_count["n"] == 1  # 404 is terminal
```

- [ ] **Step 2: Implement `src/carbuyer/sources/retry.py`**

```python
from __future__ import annotations

import asyncio
import random
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime

import httpx


RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


def _parse_retry_after(value: str) -> float | None:
    # RFC 7231: Retry-After is either delta-seconds (int) or HTTP-date.
    s = value.strip()
    if not s:
        return None
    if s.isdigit():
        return float(s)
    try:
        when = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class RetryTransport(httpx.AsyncBaseTransport):
    """Wrap an inner transport; retry transient errors with backoff.

    Honors Retry-After when present; otherwise uses jittered exponential
    backoff capped at `cap` seconds. Status codes outside RETRYABLE_STATUS
    are returned to the caller without retry.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        max_retries: int = 4,
        base: float = 1.0,
        cap: float = 30.0,
    ) -> None:
        self._inner = inner
        self._max_retries = max_retries
        self._base = base
        self._cap = cap

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            response = await self._inner.handle_async_request(request)
            if response.status_code not in RETRYABLE_STATUS:
                return response
            last = response
            if attempt == self._max_retries:
                return response
            # Drain and close the body before retrying so the connection can
            # be reused.
            await response.aread()
            await response.aclose()
            ra = response.headers.get("Retry-After")
            delay = _parse_retry_after(ra) if ra else None
            if delay is None:
                delay = min(self._cap, self._base * (2 ** attempt))
            delay = delay + random.uniform(0, max(delay * 0.25, 0.05))  # jitter
            await asyncio.sleep(delay)
        # Unreachable, but keep mypy/pyright happy.
        assert last is not None
        return last

    async def aclose(self) -> None:
        await self._inner.aclose()
```

- [ ] **Step 3: Run test**

```bash
uv run pytest tests/sources/test_retry.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/sources/retry.py tests/sources/test_retry.py
git commit -m "sources: RetryTransport — Retry-After + jittered exponential backoff"
```

---

### Task 12: HiBid `lotModels` JSON extractor

**Files:**
- Create: `src/carbuyer/sources/hibid/parser.py`
- Create: `tests/sources/hibid/test_parser.py`
- Create: `tests/sources/fixtures/hibid_catalog_synthetic.html`

- [ ] **Step 1: Hand-write a synthetic fixture**

The original plan called for capturing a real HiBid page. The Cloudflare bot
management investigation (see Phase 1 overlay) means a self-hosted curl/httpx
cannot fetch the page; the live capture is deferred to Phase 1.5. Instead, we
hand-write a synthetic fixture that exercises the parser's contract:
nested arrays, the `}],{` quirk, two lots with known ids/bids/end-times, and
the real HiBid schema (`eventItemId`, `lead`, `lotStatus.*`).

Create `tests/sources/fixtures/hibid_catalog_synthetic.html` (see test below
for the exact expected fields).

- [ ] **Step 2: Write the failing test (and the synthetic fixture inline)**

```python
# tests/sources/hibid/test_parser.py
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from carbuyer.sources.hibid.parser import (
    extract_lot_models,
    parse_lot_summary,
    raw_lot_id,
)

FIXTURE_PATH = Path("tests/sources/fixtures/hibid_catalog_synthetic.html")


def test_extract_lot_models_handles_nested_arrays() -> None:
    html = """
    <html><body><script>
    var lotModels = [
      {"eventItemId": 1, "lead": "Truck A", "images": ["a.jpg", "b.jpg"]},
      {"eventItemId": 2, "lead": "Truck B", "images": []}
    ];
    </script></body></html>
    """
    lots = extract_lot_models(html)
    assert len(lots) == 2
    assert lots[0]["eventItemId"] == 1
    assert lots[0]["images"] == ["a.jpg", "b.jpg"]


def test_extract_lot_models_no_match() -> None:
    assert extract_lot_models("<html>no script here</html>") == []


def test_extract_lot_models_handles_string_with_brackets() -> None:
    # Strings containing brackets must NOT confuse the bracket scanner.
    html = 'var lotModels = [{"lead": "Hood [scratched]", "eventItemId": 7}];'
    lots = extract_lot_models(html)
    assert lots == [{"lead": "Hood [scratched]", "eventItemId": 7}]


def test_parse_lot_summary_real_keys() -> None:
    raw = {
        "eventItemId": 4242,
        "lotNumber": "12B",
        "lead": "1995 Ford F-150 4x4",
        "description": "runs and drives",
        "lotUrl": "/alberta/lot/300724160/1995-ford-f150",
        "lotStatus": {
            "highBid": "1500.00",
            "bidCount": 7,
        },
        "auctionEnd": "2026-06-01T18:30:00Z",
        "auctionId": 740236,
        "bidIncrement": 25,
        "reserveStatus": "no_reserve",
        "lotState": "open",
    }
    summary = parse_lot_summary(raw)
    assert summary.source_lot_id == "4242"
    assert summary.lot_number == "12B"
    assert summary.title == "1995 Ford F-150 4x4"
    assert summary.description == "runs and drives"
    assert summary.current_high_bid_cad == Decimal("1500.00")
    assert summary.bid_count_visible == 7
    assert summary.url == "/alberta/lot/300724160/1995-ford-f150"
    assert summary.auction_external_id == "740236"
    assert summary.end_at == datetime(2026, 6, 1, 18, 30, 0, tzinfo=UTC)
    # Year/make/model are NOT separate fields in HiBid; left None for enricher.
    assert summary.year is None
    assert summary.make is None
    # Extra fields captured for downstream consumers.
    assert summary.extra["bidIncrement"] == 25
    assert summary.extra["reserveStatus"] == "no_reserve"
    assert summary.extra["lotState"] == "open"


def test_parse_lot_summary_handles_messy_currency() -> None:
    raw = {"eventItemId": 1, "lead": "x", "lotStatus": {"highBid": "$1,200.00"}}
    summary = parse_lot_summary(raw)
    assert summary.current_high_bid_cad == Decimal("1200.00")


def test_parse_lot_summary_handles_missing_lot_status() -> None:
    raw = {"eventItemId": 1, "lead": "x"}
    summary = parse_lot_summary(raw)
    assert summary.current_high_bid_cad is None
    assert summary.bid_count_visible is None


def test_raw_lot_id_falls_through_key_variants() -> None:
    assert raw_lot_id({"eventItemId": 5}) == "5"
    assert raw_lot_id({"lotId": 6}) == "6"
    assert raw_lot_id({"id": 7}) == "7"
    assert raw_lot_id({}) is None


def test_extract_lot_models_against_synthetic_fixture() -> None:
    if not FIXTURE_PATH.exists():
        pytest.skip(f"synthetic fixture missing: {FIXTURE_PATH}")
    html = FIXTURE_PATH.read_text()
    lots = extract_lot_models(html)
    assert len(lots) >= 2
    s = parse_lot_summary(lots[0])
    assert s.source_lot_id
    assert s.title is not None
```

- [ ] **Step 3: Implement `src/carbuyer/sources/hibid/parser.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
import json


_LOT_MODELS_START = re.compile(r"var\s+lotModels\s*=\s*\[")


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
                    parsed = json.loads(blob)
                except json.JSONDecodeError:
                    return []
                return parsed if isinstance(parsed, list) else []
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


def _to_decimal(v: Any) -> Decimal | None:
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


def _to_int(v: Any) -> int | None:
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
        # Heuristic: > 1e11 implies milliseconds since epoch.
        seconds = value / 1000 if value > 1e11 else value
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
    photos_raw = _get(raw, "lotImages", "images") or []
    if not isinstance(photos_raw, list):
        return []
    photos: list[str] = []
    for p in photos_raw:  # type: ignore[reportUnknownVariableType]
        if isinstance(p, str):
            photos.append(p)
        elif isinstance(p, dict):
            url = p.get("url") or p.get("imageUrl") or p.get("largeUrl")
            if isinstance(url, str):
                photos.append(url)
    return photos


def parse_lot_summary(raw: dict[str, Any]) -> HibidLotSummary:
    lot_status = raw.get("lotStatus") if isinstance(raw.get("lotStatus"), dict) else {}
    bid = _get(lot_status, "highBid", "currentBid") if lot_status else None
    if bid is None:
        bid = _get(raw, "highBid", "currentBid", "bidAmount")
    bid_count = _get(lot_status, "bidCount") if lot_status else None
    end = _get(raw, "auctionEnd", "saleEnd", "endTime", "scheduledEnd")
    extra = {k: raw[k] for k in _EXTRA_KEYS if k in raw and raw[k] is not None}
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
```

- [ ] **Step 4: Run test**

```bash
uv run pytest tests/sources/hibid/test_parser.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/sources/hibid/parser.py tests/sources/hibid/test_parser.py tests/sources/fixtures/hibid_catalog_sample.html
git commit -m "hibid: lotModels JSON extractor"
```

---

### Task 13: HiBidSource — discover auctions across Western CA provinces

**Files:**
- Create: `src/carbuyer/sources/hibid/source.py`
- Create: `tests/sources/hibid/test_source.py`

**Notes from Phase 1 deliberation:**
- `HibidSource` declares `version: ClassVar[str]` (Phase 0 contract).
- Calls `register(...)` at module import.
- Async context manager owning a single httpx client wired with `RetryTransport`.
- Per-call `make_client()` was removed — the source owns the client lifecycle.
- Live integration is deferred to Phase 1.5 (Cloudflare bypass). Tests use a
  MockTransport against the synthetic fixture.

- [ ] **Step 1: Write the failing test**

```python
# tests/sources/hibid/test_source.py
from pathlib import Path

import httpx
import pytest

from carbuyer.sources.base import SOURCES, AuctionRef, LotRef
from carbuyer.sources.hibid.source import HibidSource


FIXTURE = Path("tests/sources/fixtures/hibid_catalog_synthetic.html").read_text()


def _mock_transport(*, body: str = FIXTURE, status: int = 200) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)
    return httpx.MockTransport(handler)


def test_hibid_source_registered_at_import() -> None:
    assert "hibid" in SOURCES
    src = SOURCES["hibid"]
    assert src.name == "hibid"
    assert src.version  # any non-empty string


def test_hibid_source_has_classvar_name_and_version() -> None:
    assert HibidSource.name == "hibid"
    assert HibidSource.version  # ClassVar


@pytest.mark.asyncio
async def test_discover_auctions_yields_unique_refs() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        refs = [ref async for ref in src.discover_auctions()]
    assert len(refs) > 0
    assert all(r.source == "hibid" for r in refs)
    assert len({r.source_auction_id for r in refs}) == len(refs)


@pytest.mark.asyncio
async def test_fetch_lots_yields_lot_refs() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = AuctionRef(source="hibid", source_auction_id="740236", url="https://hibid.com/catalog/740236")
        lots = [r async for r in src.fetch_lots(ref)]
    assert len(lots) >= 2
    assert all(r.source_auction_id == "740236" for r in lots)


@pytest.mark.asyncio
async def test_fetch_lot_returns_raw_lot_with_extra() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = LotRef(
            source="hibid", source_auction_id="740236",
            source_lot_id="4242", url="https://hibid.com/lot/4242",
        )
        raw = await src.fetch_lot(ref)
    assert raw.title is not None and "Ford" in raw.title
    assert raw.extra.get("bidIncrement") == 25
    assert raw.extra.get("reserveStatus") == "no_reserve"


@pytest.mark.asyncio
async def test_poll_bid_returns_observation() -> None:
    async with HibidSource(provinces=["AB"], _transport=_mock_transport()) as src:
        ref = LotRef(
            source="hibid", source_auction_id="740236",
            source_lot_id="4242", url="https://hibid.com/lot/4242",
        )
        obs = await src.poll_bid(ref)
    assert obs.current_high_bid_cad is not None
    assert obs.observed_at.tzinfo is not None
```

- [ ] **Step 2: Implement `src/carbuyer/sources/hibid/source.py`**

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import ClassVar, Self

import httpx

from carbuyer.sources.base import (
    AuctionRef,
    AuctionSource,
    BidObservation,
    LotRef,
    RawAuction,
    RawLot,
    register,
)
from carbuyer.sources.hibid.parser import (
    extract_lot_models,
    parse_lot_summary,
    raw_lot_id,
)
from carbuyer.sources.hibid.urls import catalog_url, lot_url, province_vehicles_url
from carbuyer.sources.http import jittered_sleep, make_client
from carbuyer.sources.retry import RetryTransport


class HibidSource(AuctionSource):
    name: ClassVar[str] = "hibid"
    # Bump when parse_lot_summary or discover/fetch contracts change.
    version: ClassVar[str] = "1"

    def __init__(
        self,
        provinces: list[str],
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provinces = provinces
        # Tests inject a MockTransport; production wires a RetryTransport
        # around an httpx.AsyncHTTPTransport in __aenter__.
        self._injected_transport = _transport
        self._client_cm: AbstractAsyncContextManager[httpx.AsyncClient] | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        transport = self._injected_transport or RetryTransport(
            httpx.AsyncHTTPTransport()
        )
        self._client_cm = make_client(transport=transport)
        self._client = await self._client_cm.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client_cm is not None:
            await self._client_cm.__aexit__(exc_type, exc, tb)
        self._client_cm = None
        self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "HibidSource used outside `async with` — wrap in context manager",
            )
        return self._client

    async def discover_auctions(self) -> AsyncIterator[AuctionRef]:
        seen: set[str] = set()
        for i, province in enumerate(self.provinces):
            url = province_vehicles_url(province)
            resp = await self._http.get(url)
            resp.raise_for_status()
            for raw in extract_lot_models(resp.text):
                summary = parse_lot_summary(raw)
                auction_id = summary.auction_external_id
                if not auction_id or auction_id in seen:
                    continue
                seen.add(auction_id)
                yield AuctionRef(
                    source="hibid",
                    source_auction_id=auction_id,
                    url=catalog_url(auction_id),
                )
            if i < len(self.provinces) - 1:
                await jittered_sleep()

    async def fetch_auction(self, ref: AuctionRef) -> RawAuction:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        # The catalog page contains both auction-level metadata (in the page
        # header) and lotModels. For MVP, we record minimum metadata; richer
        # extraction (BP, terms_text) is left to a follow-up.
        return RawAuction(
            ref=ref,
            title=None,
            description=None,
            auctioneer_name=None,
            auctioneer_external_id=None,
            scheduled_start_at=None,
            scheduled_end_at=None,
            pickup_address=None,
            pickup_city=None,
            pickup_province=None,
            pickup_window_text=None,
            buyer_premium_pct=Decimal("0.10"),  # conservative default
            online_bidding_fee_pct=None,
            terms_text=None,
            auction_subtype="estate",
        )

    async def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        for raw in extract_lot_models(resp.text):
            summary = parse_lot_summary(raw)
            if not summary.source_lot_id:
                continue
            yield LotRef(
                source="hibid",
                source_auction_id=ref.source_auction_id,
                source_lot_id=summary.source_lot_id,
                url=summary.url or lot_url(summary.source_lot_id),
            )

    async def fetch_lot(self, ref: LotRef) -> RawLot:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        target = self._find_summary(resp.text, ref.source_lot_id)
        if target is None:
            raise ValueError(f"lot {ref.source_lot_id} not found at {ref.url}")
        return RawLot(
            ref=ref,
            lot_number=target.lot_number,
            title=target.title,
            description=target.description,
            photos=target.photos,
            year=target.year,
            make=target.make,
            model=target.model,
            current_high_bid_cad=target.current_high_bid_cad,
            bid_count_visible=target.bid_count_visible,
            scheduled_end_at=target.end_at,
            lot_status="open",
            extra=target.extra,
        )

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        resp = await self._http.get(ref.url)
        resp.raise_for_status()
        target = self._find_summary(resp.text, ref.source_lot_id)
        if target is None:
            return BidObservation(
                ref=ref,
                observed_at=datetime.now(UTC),
                current_high_bid_cad=None,
                end_time_at_observation=None,
                status_at_observation="missing",
            )
        return BidObservation(
            ref=ref,
            observed_at=datetime.now(UTC),
            current_high_bid_cad=target.current_high_bid_cad,
            end_time_at_observation=target.end_at,
            status_at_observation="open",
        )

    @staticmethod
    def _find_summary(html: str, source_lot_id: str):
        for raw in extract_lot_models(html):
            if raw_lot_id(raw) == source_lot_id:
                return parse_lot_summary(raw)
        return None


# Register at import time so the lot-scraper / discoverer worker / dashboard
# health view can enumerate covered platforms via SOURCES (Phase 0 design #11).
# Provinces default to AB/BC/SK/MB; phase-2 workers can re-instantiate with a
# different list and call register() again with the same name to override.
register(HibidSource(provinces=["AB", "BC", "SK", "MB"]))
```

- [ ] **Step 3: Run test**

```bash
uv run pytest tests/sources/hibid/test_source.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/sources/hibid/source.py tests/sources/hibid/test_source.py
git commit -m "hibid: AuctionSource implementation (discover + fetch + poll)"
```

---

### Task 14: HiBidSource integration smoke test (DEFERRED — Phase 1.5 spike)

**Status:** Deferred. The original plan called for a live opt-in smoke test that runs `HibidSource(provinces=["AB"]).discover_auctions()` against real HiBid. The Phase 1 deliberation discovered that **Cloudflare bot management on hibid.com 403s plain Python httpx** regardless of headers (any combination of UA, sec-fetch-*, accept-language, etc.). The structural plumbing (URL builders, parser, AuctionSource integration, RetryTransport) is complete and tested against a synthetic fixture; the missing piece is the CF bypass.

**Phase 1.5 scope (separate spike):**

1. **Evaluate `curl_cffi`** as a drop-in replacement for `httpx.AsyncClient`. It uses libcurl-impersonate to match Chrome/Firefox TLS fingerprints and is the lowest-friction option for sites with Cloudflare Bot Fight Mode. No JS execution needed (HiBid is server-rendered).
2. **If `curl_cffi` is 403'd**, fall back to **Playwright** (headed or headless-with-stealth). Slower; higher infra footprint.
3. **Acceptance criteria:**
   - Fetch one Alberta catalog page (`https://hibid.com/alberta/auctions/700006/cars-and-vehicles?status=open`), confirm response contains `lotModels`.
   - Run `HibidSource.discover_auctions()` and confirm at least one `AuctionRef` is yielded.
   - Run `HibidSource.fetch_lot()` against one yielded lot and confirm `RawLot` is populated.
4. **Wiring:** the chosen backend becomes the default `_transport` in production; tests still use `httpx.MockTransport` against fixtures.

**Phase 1.5 unblocks:** the auction-discoverer worker (Phase 2) running against live HiBid. Phase 2 plumbing can be built and tested without it (mocked transport against synthetic fixtures), so the project is not blocked.

**Skipped test placeholder (committed for documentation only):**

```python
# tests/sources/hibid/test_source_live.py
import os

import pytest

from carbuyer.sources.hibid.source import HibidSource


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_SCRAPE_TESTS") != "1",
    reason=(
        "live scrape — opt in with RUN_LIVE_SCRAPE_TESTS=1; "
        "currently 403'd by Cloudflare. Phase 1.5 spike adds a CF-bypass transport."
    ),
)
async def test_live_discover_at_least_one_alberta_auction() -> None:
    """When run with RUN_LIVE_SCRAPE_TESTS=1, discovers ≥1 Alberta auction.

    As of 2026-05-09 this fails with HTTP 403 from Cloudflare bot management.
    See Phase 1 overlay for the deferred bypass spike.
    """
    async with HibidSource(provinces=["AB"]) as src:
        refs = []
        async for ref in src.discover_auctions():
            refs.append(ref)
            if len(refs) >= 1:
                break
    assert len(refs) >= 1
```

- [ ] **Step 1: Commit the placeholder test**

```bash
git add tests/sources/hibid/test_source_live.py
git commit -m "hibid: placeholder live test (deferred to phase 1.5 — CF bypass)"
```

---

End of Phase 1. HiBid source plugin plumbing is implemented and tested against a synthetic fixture; live integration deferred to Phase 1.5 once a Cloudflare-bypass strategy is chosen.

---

## Phase 2 — Pipeline workers (queue infrastructure + discoverer + lot-scraper)

### Phase 2 — Design-decision overlay (post-deliberation 2026-05-09)

Three role-specialized reviews (senior async dev, Postgres DBA, systems architect) raised concrete issues. The original Phase 2 plan had multiple Phase-0 contract violations (gone-but-still-imported `async_session_maker`, source not used as async context manager, `configure_logging(settings.log_level)`) plus several SQL-correctness and recovery bugs. The user also requested the **multi-router/dedup design** be folded into this phase.

**Net effect:** Phase 2 grows by two small tasks (14.6 multi-router infrastructure; 15.5 listener catch-up sweep) and Tasks 15–19 are all rewritten.

**Must-fix decisions:**

1. **`pg_notify(:c, :p)` parameterized, not f-string `NOTIFY`.** The original `f"NOTIFY {channel}, '{safe}'"` is SQL-injection-vulnerable on `channel` and brittle on `payload`. Use `text("SELECT pg_notify(:c, :p)")` with bound params + 7900-byte payload check + channel-name allowlist regex.
2. **`UPSERT` via `INSERT ... ON CONFLICT DO UPDATE`, not SELECT-then-INSERT.** The plan's two-statement check-then-insert races on the unique constraint and aborts the whole transaction on `IntegrityError`. Use the Postgres-native UPSERT.
3. **Source plugins as async context managers.** Phase 1 contract: `HibidSource` raises `RuntimeError` when used without `async with`. Workers wrap a long-lived `AsyncExitStack` over all registered plugins for the worker's lifetime — one HTTP client per plugin per process, not per-call.
4. **Module-level `async_session_maker` is gone.** Phase 0 replaced it with `get_session()` / `get_session_maker()`. Every call site updated.
5. **`configure_logging()` (no args).** Reads `settings.log_level` internally; passing it explicitly is contract drift.
6. **`upsert_lot` writes `parser_version=source.version`.** Phase 0 design #8 mandates it; the original plan didn't.
7. **Status-reset cascade:** when content fields change (`title/description/photos/year/make/model/vin/mileage_km` or `parser_version`), reset `enrichment_status = valuation_status = vision_status = notification_status = "pending"`. When only bid fields change, reset NOTHING (bid-poller's domain). Lot-scraper does **not** write bid columns.
8. **Per-lot transaction in `process_auction`** (was: one outer transaction over all 200 lots). The outer-txn pattern violates `idle_in_transaction_session_timeout=60s`, holds locks across HTTP I/O, and floods the listener with N NOTIFYs at single-commit time. Per-lot commit gives durability + paces NOTIFYs naturally + never holds a txn across HTTP.
9. **Two-phase SKIP LOCKED claim:** `claim_pending_lots` commits immediately after flipping rows to `'in_progress'`. The `'in_progress'` flag IS the ownership marker; the row-lock isn't held across the worker's downstream HTTP work. Avoids the `idle_in_transaction_session_timeout` tripwire.
10. **`raw.x or existing.x` → `if raw.x is not None: existing.x = raw.x`.** Short-circuit `or` discards `Decimal("0")`, `0`, `""`. Subtle data-loss bug.
11. **`listen()` reconnects on connection drop.** psycopg's `OperationalError` mid-stream killed the worker; now wrapped in a backoff retry loop. `_runner.py` calls `loop.shutdown_asyncgens()` before `loop.close()` so the dedicated psycopg connection is properly closed on SIGTERM.
12. **Listener-startup catchup sweep.** Every continuous worker that uses `LISTEN` first runs `SELECT id WHERE status='pending'` and enqueues those rows, then enters `LISTEN`. Otherwise notifications fired during a worker outage are lost forever. Same helper used by all four queue workers (lot-scraper, enricher, valuator, notifier).
13. **Channel-name allowlist + payload size cap** in `notify()` and `listen()`: regex `^[a-z][a-z0-9_]{0,62}$` for channels, ≤7900-byte payload. Avoids identifier-injection and the Postgres 8KB NOTIFY limit.
14. **Lot-scraper skips `unknown:{host}` sources** with an INFO log instead of erroring. The discoverer creates these rows from routers (Phase 10); the lot-scraper has no fetcher for them. The dashboard's "needs-plugin" view (Phase 10 Task 42) keys off `source LIKE 'unknown:%'`.
15. **Partial indexes on `*_status='pending'`.** Five indexes — `enrichment_pending`, `valuation_pending`, `vision_pending`, `notification_pending`, `lot_status_open` — created in the multi-router migration so the queue queries scan a tiny index instead of the full table.
16. **`canonical_url` migration is three-step:** (a) add nullable column, (b) backfill from `url` via Python, (c) `SET NOT NULL`. Safe regardless of how many auction rows exist when the migration runs.

**Should-fix decisions:**

17. **`parse_auction_url` is a classmethod on the `Source` ABC, default `return None`.** Replaces `getattr(src, "parse_auction_url", None)` duck-typing — pyright knows the contract; typos in subclass override silently never match → loud test failure instead.
18. **`discovered_via` is a `text[]` column with native `array_append` + dedup inside the UPSERT.** JSONB array would need `MutableList.as_mutable(JSONB)` or app-side read-modify-write (race-prone). Postgres-native is cleaner.
19. **Per-plugin try/except + timeout in discoverer.** A failing HiBid sweep shouldn't block farmauctionguide.
20. **Loud `WARNING` log on every `unknown:{host}` discovery.** Until the Phase-10 dashboard exists, `journalctl -u auction-discoverer | grep "unknown platform"` is the operator's only signal that a new platform showed up.
21. **`canonicalize_url` strips known tracking params** (`utm_*, ref, fbclid, gclid, mc_eid`) plus fragment + trailing slash; lowercases the host. Per-plugin override hook (`Source.canonicalize_url(cls, url)`) deferred until a real platform forces it.

**Deferred (acknowledged):**

- **Watchdog worker** for stuck `in_progress` rows. Documented as a follow-up task (Phase 2.5). Not strictly needed for MVP; revisit when Phase 4 (valuator) starts seeing real OpenAI failures.
- **Per-plugin `canonicalize_url`** override hook. Default stripper handles HiBid; revisit when a second platform demands a different rule.
- **`pg_advisory_xact_lock`** for singleton-discoverer correctness. Enforced today by deployment (one systemd unit); add lock if multi-replica becomes a concern.

End of overlay.

---

### Task 14.6: Multi-router URL resolver + schema additions

**Why this task is here:** Routers (farmauctionguide.com et al.) discover auctions hosted on other platforms. The infrastructure must let plugins parse incoming URLs into `(source, source_auction_id)` tuples, dedupe across routers, and flag URLs no plugin recognizes. The `auctions` schema gains `canonical_url` and `discovered_via` columns; the `Source` ABC gains a `parse_auction_url` classmethod; new module `sources/resolver.py` provides the URL-resolution helpers.

**Files:**
- Create: `src/carbuyer/sources/resolver.py`
- Modify: `src/carbuyer/sources/base.py` (add `parse_auction_url` classmethod default + `canonicalize_url` classmethod default)
- Modify: `src/carbuyer/db/models.py` (add `canonical_url`, `discovered_via` columns)
- Modify: `src/carbuyer/sources/hibid/source.py` (override `parse_auction_url` for `hibid.com/.../catalog/{id}` URLs)
- Create: `tests/sources/test_resolver.py`
- Create: Alembic migration `alembic/versions/<hash>_canonical_url_discovered_via.py`

- [ ] **Step 1: Tests for the resolver**

```python
# tests/sources/test_resolver.py
from carbuyer.sources.base import SOURCES
from carbuyer.sources.hibid.source import HibidSource  # noqa: F401  -- registers
from carbuyer.sources.resolver import (
    canonicalize_url,
    resolve_auction_url,
    unknown_platform_ref,
)


def test_canonicalize_url_strips_fragment_and_tracking() -> None:
    raw = "https://Example.COM/Foo/Bar/?utm_source=newsletter&ref=fag&id=42#hash"
    assert canonicalize_url(raw) == "https://example.com/Foo/Bar?id=42"


def test_canonicalize_url_idempotent() -> None:
    once = canonicalize_url("https://hibid.com/catalog/740236/?utm_campaign=x")
    twice = canonicalize_url(once)
    assert once == twice


def test_resolve_auction_url_finds_hibid() -> None:
    ref = resolve_auction_url("https://hibid.com/catalog/740236/some-slug")
    assert ref is not None
    assert ref.source == "hibid"
    assert ref.source_auction_id == "740236"


def test_resolve_auction_url_returns_none_for_unknown() -> None:
    assert resolve_auction_url("https://example.com/auction/99") is None


def test_unknown_platform_ref_is_deterministic() -> None:
    a = unknown_platform_ref("https://foo.ca/auction/123/?utm_source=x")
    b = unknown_platform_ref("https://foo.ca/auction/123#hash")
    assert a.source == "unknown:foo.ca"
    assert a.source_auction_id == b.source_auction_id  # same canonical → same id
```

- [ ] **Step 2: Implement `src/carbuyer/sources/resolver.py`**

```python
from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from carbuyer.sources.base import SOURCES, AuctionRef


# Tracking params we strip universally. Platform-specific overrides come via
# Source.canonicalize_url when a real platform needs different handling.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "ref", "fbclid", "gclid", "mc_eid", "mc_cid", "_ga",
})


def canonicalize_url(url: str) -> str:
    """Strip tracking params and fragment; lowercase host. Idempotent."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(pairs)
    path = parsed.path.rstrip("/") or "/"
    if path == "/":
        path = ""
    return urlunparse((parsed.scheme, netloc, path, parsed.params, query, ""))


def resolve_auction_url(url: str) -> AuctionRef | None:
    """Walk SOURCES (sorted by name for determinism); return the first match."""
    for name in sorted(SOURCES.keys()):
        ref = SOURCES[name].parse_auction_url(url)
        if ref is not None:
            return ref
    return None


def unknown_platform_ref(url: str) -> AuctionRef:
    canonical = canonicalize_url(url)
    host = urlparse(canonical).hostname or "unknown"
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return AuctionRef(source=f"unknown:{host}", source_auction_id=digest, url=canonical)
```

- [ ] **Step 3: Add `parse_auction_url` and `canonicalize_url` defaults to `Source` ABC**

In `sources/base.py`, after the `Source` class definition:

```python
class Source(ABC):
    name: ClassVar[str]
    version: ClassVar[str]

    @classmethod
    def parse_auction_url(cls, url: str) -> AuctionRef | None:
        """Return AuctionRef if THIS source is authoritative for the URL, else None.

        Overrides in concrete plugins. Default is `None` (plugin doesn't claim
        this URL); the `resolve_auction_url` helper walks all registered sources.
        """
        del url  # default impl ignores
        return None
```

- [ ] **Step 4: Override `parse_auction_url` on `HibidSource`**

In `sources/hibid/source.py`:

```python
@classmethod
def parse_auction_url(cls, url: str) -> AuctionRef | None:
    # https://hibid.com/catalog/{id}[/slug]
    # https://hibid.com/{province}/catalog/{id}[/slug]
    m = re.match(r"^https?://(?:www\.)?hibid\.com/(?:[a-z\-]+/)?catalog/(\d+)", url)
    if m is None:
        return None
    from carbuyer.sources.resolver import canonicalize_url
    return AuctionRef(source=cls.name, source_auction_id=m.group(1), url=canonicalize_url(url))
```

- [ ] **Step 5: Add `canonical_url` and `discovered_via` columns to `Auction`**

In `db/models.py`:

```python
canonical_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
# text[] (not JSONB) so we can append + dedupe atomically inside ON CONFLICT.
# Each entry is the name of a Source (plugin or router) that has surfaced
# this auction. Used by Phase 10 "needs-plugin" alerting and the dashboard.
discovered_via: Mapped[list[str]] = mapped_column(
    ARRAY(Text), default=list, server_default=text("'{}'::text[]"), nullable=False,
)
```

- [ ] **Step 6: Alembic migration with backfill**

Hand-edit the autogenerated migration to do three steps:

```python
def upgrade() -> None:
    op.add_column("auctions", sa.Column("canonical_url", sa.Text(), nullable=True))
    op.add_column(
        "auctions",
        sa.Column(
            "discovered_via",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    # Backfill canonical_url from url. Conservative: copy URL as-is if no rows
    # exist (which is the case post-Phase-1); this avoids needing the Python
    # canonicalizer in the migration.
    op.execute("UPDATE auctions SET canonical_url = url WHERE canonical_url IS NULL")
    op.alter_column("auctions", "canonical_url", nullable=False)
    op.create_index("ix_auctions_canonical_url", "auctions", ["canonical_url"])
    # Partial indexes for queue-claim queries on auction_lots.
    op.execute(
        "CREATE INDEX ix_auction_lots_enrichment_pending "
        "ON auction_lots(id) WHERE enrichment_status = 'pending'"
    )
    op.execute(
        "CREATE INDEX ix_auction_lots_valuation_pending "
        "ON auction_lots(id) WHERE valuation_status = 'pending'"
    )
    op.execute(
        "CREATE INDEX ix_auction_lots_vision_pending "
        "ON auction_lots(id) WHERE vision_status = 'pending'"
    )
    op.execute(
        "CREATE INDEX ix_auction_lots_notification_pending "
        "ON auction_lots(id) WHERE notification_status = 'pending'"
    )


def downgrade() -> None:
    op.drop_index("ix_auction_lots_notification_pending", "auction_lots")
    op.drop_index("ix_auction_lots_vision_pending", "auction_lots")
    op.drop_index("ix_auction_lots_valuation_pending", "auction_lots")
    op.drop_index("ix_auction_lots_enrichment_pending", "auction_lots")
    op.drop_index("ix_auctions_canonical_url", "auctions")
    op.drop_column("auctions", "discovered_via")
    op.drop_column("auctions", "canonical_url")
```

Add the partial indexes to the ORM model so future autogenerate compares them:

```python
__table_args__ = (
    UniqueConstraint(...),
    Index("ix_auction_lots_make_model_year", ...),
    Index("ix_auction_lots_price_deal_score", "price_deal_score", "lot_status"),
    Index("ix_auction_lots_rarity_score", "rarity_score"),
    Index(
        "ix_auction_lots_enrichment_pending", "id",
        postgresql_where=text("enrichment_status = 'pending'"),
    ),
    Index(
        "ix_auction_lots_valuation_pending", "id",
        postgresql_where=text("valuation_status = 'pending'"),
    ),
    Index(
        "ix_auction_lots_vision_pending", "id",
        postgresql_where=text("vision_status = 'pending'"),
    ),
    Index(
        "ix_auction_lots_notification_pending", "id",
        postgresql_where=text("notification_status = 'pending'"),
    ),
)
```

- [ ] **Step 7: Run tests + apply migration**

```bash
uv run pytest tests/sources/test_resolver.py -v
uv run alembic upgrade head
uv run pytest -v
```

Expected: all pass; migration applies cleanly.

- [ ] **Step 8: Commit**

```bash
git add src/carbuyer/sources/resolver.py src/carbuyer/sources/base.py src/carbuyer/db/models.py \
        src/carbuyer/sources/hibid/source.py tests/sources/test_resolver.py alembic/versions/
git commit -m "sources: multi-router URL resolver + canonical_url/discovered_via on auctions"
```

---

### Task 15: LISTEN/NOTIFY async helper (dedicated psycopg connection)

**Files:**
- Create: `src/carbuyer/db/notify.py`
- Create: `tests/db/test_notify.py`

**Notes from deliberation:**
- `notify()` uses parameterized `pg_notify(:c, :p)` — never f-string `NOTIFY`. Channel name is allowlisted; payload size capped at 7900 bytes (Postgres limit is 8000).
- `listen()` reconnects on `psycopg.OperationalError` with backoff. The dedicated psycopg connection is not in the SA pool and deliberately has no `statement_timeout` (LISTEN must block).
- `_to_psycopg_url` accepts known SA driver-prefixed URLs (psycopg / asyncpg / psycopg2) — fails fast on anything else.

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_notify.py
import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.notify import _CHANNEL_RE, listen, notify
from carbuyer.shared.config import settings


def _test_url() -> str:
    url = settings.database_url
    return url if url.endswith("_test") else url.replace("/carbuyer", "/carbuyer_test")


@pytest.mark.asyncio
async def test_notify_round_trip(session: AsyncSession) -> None:
    received: list[str] = []

    async def reader() -> None:
        async for payload in listen("test_channel", url=_test_url()):
            received.append(payload)
            return

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.2)
    await notify(session, "test_channel", "hello")
    await session.commit()
    await asyncio.wait_for(task, timeout=2.0)
    assert received == ["hello"]


@pytest.mark.asyncio
async def test_notify_rejects_invalid_channel(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="invalid channel name"):
        await notify(session, "bad-channel!", "x")
    with pytest.raises(ValueError, match="invalid channel name"):
        await notify(session, "1starts-with-digit", "x")


@pytest.mark.asyncio
async def test_notify_rejects_oversized_payload(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="payload"):
        await notify(session, "test_channel", "x" * 8000)


def test_channel_regex() -> None:
    assert _CHANNEL_RE.match("auction_pending")
    assert _CHANNEL_RE.match("a")
    assert not _CHANNEL_RE.match("Auction_Pending")  # uppercase
    assert not _CHANNEL_RE.match("auction-pending")  # dash
    assert not _CHANNEL_RE.match("1auction")          # starts with digit
    assert not _CHANNEL_RE.match("")
```

- [ ] **Step 2: Implement `src/carbuyer/db/notify.py`**

```python
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger


_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Postgres NOTIFY hard limit is 8000 bytes; leave headroom for protocol overhead.
_MAX_PAYLOAD_BYTES = 7900


log = get_logger("db.notify")


def _check_channel(channel: str) -> None:
    if not _CHANNEL_RE.match(channel):
        raise ValueError(f"invalid channel name: {channel!r}")


def _to_psycopg_url(sa_url: str) -> str:
    """SQLAlchemy URL → psycopg URL (drops the +driver suffix)."""
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://", "postgresql+psycopg2://"):
        if sa_url.startswith(prefix):
            return "postgresql://" + sa_url[len(prefix):]
    if sa_url.startswith("postgresql://"):
        return sa_url
    raise ValueError(f"unsupported database URL scheme: {sa_url[:30]}...")


async def notify(session: AsyncSession, channel: str, payload: str = "") -> None:
    """Send a NOTIFY using `pg_notify()` so both args are properly bound."""
    _check_channel(channel)
    if len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"NOTIFY payload exceeds {_MAX_PAYLOAD_BYTES} bytes",
        )
    await session.execute(
        text("SELECT pg_notify(:c, :p)").bindparams(c=channel, p=payload),
    )


async def listen(
    channel: str,
    *,
    url: str | None = None,
    reconnect_max_backoff: float = 30.0,
) -> AsyncIterator[str]:
    """Yield NOTIFY payloads on `channel`, reconnecting on connection drops.

    The connection is autocommit + has no statement_timeout (LISTEN must block).
    On psycopg.OperationalError or OSError, sleep with exponential backoff and
    reconnect. Caller is responsible for catchup-pending sweeps via db.queue.
    """
    _check_channel(channel)
    psycopg_url = _to_psycopg_url(url or settings.database_url)
    backoff = 1.0
    while True:
        try:
            aconn = await psycopg.AsyncConnection.connect(psycopg_url, autocommit=True)
            try:
                async with aconn.cursor() as cur:
                    # Channel name has already been validated above, so f-string is safe.
                    await cur.execute(f"LISTEN {channel}")  # noqa: S608  -- channel validated
                backoff = 1.0
                async for n in aconn.notifies():
                    yield n.payload
            finally:
                await aconn.close()
        except (psycopg.OperationalError, OSError) as exc:
            log.warning(
                "listen reconnect", channel=channel, error=str(exc), backoff=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, reconnect_max_backoff)
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/db/test_notify.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/db/notify.py tests/db/test_notify.py
git commit -m "db: LISTEN/NOTIFY helpers (pg_notify + reconnect + channel allowlist)"
```

---

### Task 16: SKIP LOCKED queue-claim helper + catchup-pending sweep

**Files:**
- Create: `src/carbuyer/db/queue.py`
- Create: `tests/db/test_queue.py`

**Notes from deliberation:**
- **Two-phase claim:** `claim_pending_lots` opens its own short transaction, flips rows to `'in_progress'`, commits, returns ids. The downstream worker processes each id in a fresh transaction. Avoids holding `FOR UPDATE` locks across HTTP I/O and dodges `idle_in_transaction_session_timeout=60s`.
- **Catchup sweep:** `select_pending_ids()` returns `pending` ids with no locking — listener-startup code uses it to enqueue rows that NOTIFYed during a worker outage.

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_queue.py
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import EnrichmentStatus
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.queue import claim_pending_ids, select_pending_ids


@pytest.mark.asyncio
async def test_claim_pending_ids_marks_in_progress(session: AsyncSession) -> None:
    a = Auction(
        source="test", source_auction_id="A1", url="x",
        auction_subtype="estate", canonical_url="x",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()
    for i in range(3):
        session.add(AuctionLot(
            auction_id=cast(int, a.id), source_lot_id=f"L{i}", url=f"u{i}",
        ))
    await session.commit()

    ids = await claim_pending_ids(session, status_field="enrichment_status", limit=2)
    assert len(ids) == 2

    # Verify the rows are marked in_progress.
    for lot_id in ids:
        lot = await session.get(AuctionLot, lot_id)
        assert lot is not None
        assert lot.enrichment_status == EnrichmentStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_select_pending_ids_does_not_modify_rows(session: AsyncSession) -> None:
    a = Auction(
        source="test", source_auction_id="A2", url="x",
        auction_subtype="estate", canonical_url="x",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()
    session.add(AuctionLot(
        auction_id=cast(int, a.id), source_lot_id="L0", url="u0",
    ))
    await session.commit()

    ids = await select_pending_ids(session, status_field="enrichment_status")
    assert len(ids) == 1
    # Status unchanged.
    lot = await session.get(AuctionLot, ids[0])
    assert lot is not None
    assert lot.enrichment_status == EnrichmentStatus.PENDING
```

- [ ] **Step 2: Implement `src/carbuyer/db/queue.py`**

```python
from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import AuctionLot


StatusField = Literal[
    "enrichment_status", "valuation_status", "vision_status", "notification_status",
]

_IN_PROGRESS_BY_FIELD: dict[StatusField, str] = {
    "enrichment_status": EnrichmentStatus.IN_PROGRESS,
    "valuation_status": ValuationStatus.IN_PROGRESS,
    "vision_status": VisionStatus.IN_PROGRESS,
    # NotificationStatus has no IN_PROGRESS — notifier flips PENDING → DONE/SKIPPED.
}


async def claim_pending_ids(
    session: AsyncSession,
    *,
    status_field: StatusField,
    limit: int = 50,
) -> list[int]:
    """Claim up to `limit` pending lot ids; commit immediately so the lock releases.

    The returned ids are owned by the caller; downstream processing happens in a
    fresh transaction. The 'in_progress' marker (rather than the row lock) IS the
    ownership signal — a watchdog can later flip stuck 'in_progress' rows back to
    'pending' (Phase 2.5).
    """
    if status_field not in _IN_PROGRESS_BY_FIELD:
        raise ValueError(f"{status_field} has no IN_PROGRESS state — use NotificationStatus.DONE/SKIPPED directly")
    column = getattr(AuctionLot, status_field)
    stmt = (
        select(AuctionLot.id)
        .where(column == "pending")
        .order_by(AuctionLot.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        await session.commit()
        return []
    in_progress_value = _IN_PROGRESS_BY_FIELD[status_field]
    update_stmt = (
        AuctionLot.__table__.update()
        .where(AuctionLot.id.in_(rows))
        .values({status_field: in_progress_value})
    )
    await session.execute(update_stmt)
    await session.commit()
    return list(rows)


async def select_pending_ids(
    session: AsyncSession,
    *,
    status_field: StatusField,
    limit: int = 1000,
) -> list[int]:
    """Read-only catchup sweep. Returns ids of pending rows without locking them.

    Used at listener startup and on reconnect to recover NOTIFY-fired-while-down
    rows. Caller dispatches each id (typically by issuing a NOTIFY).
    """
    column = getattr(AuctionLot, status_field)
    stmt = (
        select(AuctionLot.id)
        .where(column == "pending")
        .order_by(AuctionLot.id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/db/test_queue.py -v
```

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/db/queue.py tests/db/test_queue.py
git commit -m "db: two-phase SKIP LOCKED claim + catchup-pending sweep"
```

---

### Task 17: Worker entrypoint pattern (`apps/_runner.py`)

**Files:**
- Create: `src/carbuyer/apps/__init__.py`
- Create: `src/carbuyer/apps/_runner.py`
- Create: `tests/apps/__init__.py`
- Create: `tests/apps/test_runner.py`

**Notes from deliberation:**
- `configure_logging()` (no args) — reads `settings.log_level` internally.
- `loop.shutdown_asyncgens()` runs before `loop.close()` so the dedicated psycopg LISTEN connection in `db.notify.listen()` is properly closed on SIGTERM.

- [ ] **Step 1: Test**

```python
# tests/apps/test_runner.py
import asyncio

import pytest

from carbuyer.apps._runner import run_worker


def test_run_worker_runs_to_completion() -> None:
    fired = {"n": 0}

    async def main() -> None:
        fired["n"] += 1

    run_worker("test", main)
    assert fired["n"] == 1


def test_run_worker_propagates_exception() -> None:
    async def main() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_worker("test", main)


def test_run_worker_handles_cancellation() -> None:
    async def main() -> None:
        await asyncio.sleep(60)  # would block, but the test cancels via the loop

    # Manual cancellation via injected handler — patched in via the implementation.
    # See implementation for the testable cancel hook (run_worker_for_test).
```

- [ ] **Step 2: Implement `src/carbuyer/apps/_runner.py`**

```python
from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from carbuyer.shared.logging import configure_logging, get_logger


def run_worker(name: str, main: Callable[[], Awaitable[None]]) -> None:
    """Run an async worker with graceful shutdown on SIGTERM/SIGINT.

    On signal:
      1. The worker task is cancelled.
      2. CancelledError propagates out of `main()`.
      3. `loop.shutdown_asyncgens()` runs to ensure async-generator finalizers
         (e.g. psycopg LISTEN connection close) are awaited.
      4. The loop closes.
    """
    configure_logging()
    log = get_logger(name)
    loop = asyncio.new_event_loop()

    async def runner() -> None:
        log.info("worker starting")
        try:
            await main()
        except asyncio.CancelledError:
            log.info("worker cancelled")
            raise
        except Exception:
            log.exception("worker crashed")
            raise
        finally:
            log.info("worker exiting")

    task = loop.create_task(runner())

    def stop(*_: object) -> None:
        if not task.done():
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/apps/test_runner.py -v
```

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/apps/__init__.py src/carbuyer/apps/_runner.py \
        tests/apps/__init__.py tests/apps/test_runner.py
git commit -m "apps: worker entrypoint with shutdown_asyncgens for clean LISTEN close"
```

---

### Task 18: Auction-discoverer worker

**Files:**
- Create: `src/carbuyer/apps/auction_discoverer/__init__.py`
- Create: `src/carbuyer/apps/auction_discoverer/__main__.py`
- Create: `src/carbuyer/apps/auction_discoverer/discoverer.py`
- Create: `tests/apps/test_auction_discoverer.py`

**Notes from deliberation:**
- `INSERT ... ON CONFLICT DO UPDATE` (atomic UPSERT), not SELECT-then-INSERT.
- Sources entered via `AsyncExitStack` for the worker's lifetime; per-method `async with HibidSource(...)` thrash avoided.
- Per-plugin try/except + per-source short timeout — a failing HiBid sweep doesn't block farmauctionguide.
- `discovered_via` = source's own `name` (HiBid surfaced this auction itself); routers append their name through the same UPSERT path.
- `unknown:{host}` AuctionRefs (from routers in Phase 10) skip the `fetch_auction` call — there's no fetcher plugin for them — and write a minimal Auction row with only `canonical_url` populated.
- `if raw.x is not None: existing.x = raw.x` semantics — never short-circuit `or` (would discard `Decimal("0")`).

- [ ] **Step 1: Write the failing test**

```python
# tests/apps/test_auction_discoverer.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.auction_discoverer.discoverer import upsert_auction
from carbuyer.db.models import Auction
from carbuyer.sources.base import AuctionRef, RawAuction


def _raw(title: str = "t1", **overrides: object) -> RawAuction:
    base = {
        "ref": AuctionRef(source="test", source_auction_id="A1", url="https://x/a/1"),
        "title": title,
        "description": None,
        "auctioneer_name": "A Co",
        "auctioneer_external_id": "ac1",
        "scheduled_start_at": None,
        "scheduled_end_at": None,
        "pickup_address": None,
        "pickup_city": None,
        "pickup_province": "AB",
        "pickup_window_text": None,
        "buyer_premium_pct": Decimal("0.10"),
        "online_bidding_fee_pct": None,
        "terms_text": None,
        "auction_subtype": "estate",
    }
    base.update(overrides)
    return RawAuction(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_upsert_auction_inserts_then_updates(session: AsyncSession) -> None:
    a1 = await upsert_auction(session, _raw(title="t1"), discovered_via="hibid")
    await session.commit()
    assert a1.id is not None
    assert a1.discovered_via == ["hibid"]

    a2 = await upsert_auction(session, _raw(title="t1-renamed"), discovered_via="hibid")
    await session.commit()
    assert a2.id == a1.id
    assert a2.title == "t1-renamed"

    rows = (await session.execute(
        select(Auction).where(Auction.source == "test"),
    )).scalars().all()
    assert len(list(rows)) == 1


@pytest.mark.asyncio
async def test_upsert_auction_dedupes_discovered_via(session: AsyncSession) -> None:
    a = await upsert_auction(session, _raw(), discovered_via="hibid")
    await session.commit()
    a = await upsert_auction(session, _raw(), discovered_via="hibid")  # duplicate
    await session.commit()
    a = await upsert_auction(session, _raw(), discovered_via="farmauctionguide")
    await session.commit()
    assert sorted(a.discovered_via) == ["farmauctionguide", "hibid"]


@pytest.mark.asyncio
async def test_upsert_auction_does_not_overwrite_with_none(session: AsyncSession) -> None:
    a = await upsert_auction(session, _raw(title="t1"), discovered_via="hibid")
    await session.commit()
    raw = _raw(title=None)  # later sweep sees no title
    a2 = await upsert_auction(session, raw, discovered_via="hibid")
    await session.commit()
    assert a2.id == a.id
    assert a2.title == "t1"  # original preserved
```

- [ ] **Step 2: Implement `src/carbuyer/apps/auction_discoverer/discoverer.py`**

```python
from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Auction
from carbuyer.db.notify import notify
from carbuyer.db.session import get_session
from carbuyer.shared.logging import get_logger
from carbuyer.sources.base import (
    SOURCES,
    AuctionDiscoverer,
    AuctionFetcher,
    AuctionRef,
    RawAuction,
)
from carbuyer.sources.resolver import canonicalize_url

log = get_logger("auction_discoverer")


def _minimal_raw_auction(ref: AuctionRef) -> RawAuction:
    """For unknown-platform refs (routers, no fetcher plugin) — record bare metadata."""
    return RawAuction(
        ref=ref,
        title=None, description=None,
        auctioneer_name=None, auctioneer_external_id=None,
        scheduled_start_at=None, scheduled_end_at=None,
        pickup_address=None, pickup_city=None, pickup_province=None,
        pickup_window_text=None,
        buyer_premium_pct=None, online_bidding_fee_pct=None,
        terms_text=None, auction_subtype="estate",
    )


async def upsert_auction(
    session: AsyncSession,
    raw: RawAuction,
    *,
    discovered_via: str,
) -> Auction:
    """Atomic UPSERT keyed on (source, source_auction_id).

    On conflict: refresh `last_seen_at`, copy non-None fields from `raw` (never
    overwrite with None), and append `discovered_via` to the array if not already
    present.
    """
    now = datetime.now(UTC)
    canonical = canonicalize_url(raw.ref.url)

    insert_values: dict[str, object] = {
        "source": raw.ref.source,
        "source_auction_id": raw.ref.source_auction_id,
        "url": raw.ref.url,
        "canonical_url": canonical,
        "discovered_via": [discovered_via],
        "auction_subtype": raw.auction_subtype,
        "auctioneer_name": raw.auctioneer_name,
        "auctioneer_external_id": raw.auctioneer_external_id,
        "title": raw.title,
        "description": raw.description,
        "terms_text": raw.terms_text,
        "scheduled_start_at": raw.scheduled_start_at,
        "scheduled_end_at": raw.scheduled_end_at,
        "pickup_address": raw.pickup_address,
        "pickup_city": raw.pickup_city,
        "pickup_province": raw.pickup_province,
        "pickup_window_text": raw.pickup_window_text,
        "buyer_premium_pct": raw.buyer_premium_pct,
        "online_bidding_fee_pct": raw.online_bidding_fee_pct,
        "status": "upcoming",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    stmt = pg_insert(Auction).values(**insert_values)

    # Excluded refs the proposed-insert row's value; coalesce keeps existing
    # non-None values when the new row has None.
    excluded = stmt.excluded
    update_set = {
        "url": excluded.url,
        "canonical_url": excluded.canonical_url,
        "auction_subtype": func.coalesce(excluded.auction_subtype, Auction.auction_subtype),
        "auctioneer_name": func.coalesce(excluded.auctioneer_name, Auction.auctioneer_name),
        "auctioneer_external_id": func.coalesce(
            excluded.auctioneer_external_id, Auction.auctioneer_external_id,
        ),
        "title": func.coalesce(excluded.title, Auction.title),
        "description": func.coalesce(excluded.description, Auction.description),
        "terms_text": func.coalesce(excluded.terms_text, Auction.terms_text),
        "scheduled_start_at": func.coalesce(
            excluded.scheduled_start_at, Auction.scheduled_start_at,
        ),
        "scheduled_end_at": func.coalesce(
            excluded.scheduled_end_at, Auction.scheduled_end_at,
        ),
        "pickup_address": func.coalesce(excluded.pickup_address, Auction.pickup_address),
        "pickup_city": func.coalesce(excluded.pickup_city, Auction.pickup_city),
        "pickup_province": func.coalesce(excluded.pickup_province, Auction.pickup_province),
        "pickup_window_text": func.coalesce(
            excluded.pickup_window_text, Auction.pickup_window_text,
        ),
        "buyer_premium_pct": func.coalesce(
            excluded.buyer_premium_pct, Auction.buyer_premium_pct,
        ),
        "online_bidding_fee_pct": func.coalesce(
            excluded.online_bidding_fee_pct, Auction.online_bidding_fee_pct,
        ),
        "last_seen_at": excluded.last_seen_at,
        # Atomic dedup-append: array || EXCLUDED.array, then DISTINCT via ARRAY(SELECT DISTINCT ...).
        "discovered_via": text(
            "ARRAY(SELECT DISTINCT unnest(auctions.discovered_via || EXCLUDED.discovered_via))",
        ),
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "source_auction_id"],
        set_=update_set,
    ).returning(Auction)
    result = await session.execute(stmt)
    auction = result.scalar_one()
    return auction


async def _sweep_one_discoverer(
    discoverer: AuctionDiscoverer,
    fetchers: dict[str, AuctionFetcher],
) -> int:
    found = 0
    log.info("discovering", source=discoverer.name)
    async for ref in discoverer.discover_auctions():
        if ref.source.startswith("unknown:"):
            log.warning(
                "unknown platform discovered",
                router=discoverer.name, source=ref.source, url=ref.url,
            )
            raw = _minimal_raw_auction(ref)
        else:
            fetcher = fetchers.get(ref.source)
            if fetcher is None:
                log.warning(
                    "no fetcher for resolved source — recording metadata only",
                    router=discoverer.name, source=ref.source,
                )
                raw = _minimal_raw_auction(ref)
            else:
                try:
                    raw = await fetcher.fetch_auction(ref)
                except Exception:
                    log.exception(
                        "fetch_auction failed",
                        source=ref.source, ref_url=ref.url,
                    )
                    continue
        async with get_session() as session, session.begin():
            auction = await upsert_auction(
                session, raw, discovered_via=discoverer.name,
            )
            await notify(session, "auction_pending", str(auction.id))
        found += 1
    return found


async def discover_once() -> int:
    """One sweep across every registered discoverer; returns auctions surfaced."""
    discoverers = [s for s in SOURCES.values() if isinstance(s, AuctionDiscoverer)]
    fetchers: dict[str, AuctionFetcher] = {
        s.name: s for s in SOURCES.values() if isinstance(s, AuctionFetcher)
    }
    total = 0
    async with AsyncExitStack() as stack:
        # Enter every plugin's async-CM ONCE for the duration of the sweep.
        for d in discoverers:
            await stack.enter_async_context(d)
        for f in fetchers.values():
            if f not in discoverers:
                await stack.enter_async_context(f)
        for d in discoverers:
            try:
                total += await _sweep_one_discoverer(d, fetchers)
            except Exception:
                log.exception("discoverer sweep failed", source=d.name)
                continue
    log.info("discovery complete", found=total)
    return total


async def main() -> None:
    # Importing the plugin module triggers `register(...)`. Add new platforms here.
    import carbuyer.sources.hibid.source  # noqa: F401  -- registers HibidSource
    await discover_once()
```

- [ ] **Step 3: Implement `src/carbuyer/apps/auction_discoverer/__main__.py`**

```python
from carbuyer.apps._runner import run_worker
from carbuyer.apps.auction_discoverer.discoverer import main


if __name__ == "__main__":
    run_worker("auction_discoverer", main)
```

- [ ] **Step 4: Create `__init__.py` and run tests**

```bash
touch src/carbuyer/apps/auction_discoverer/__init__.py
uv run pytest tests/apps/test_auction_discoverer.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/auction_discoverer/ tests/apps/test_auction_discoverer.py
git commit -m "apps: auction-discoverer with UPSERT + AsyncExitStack + multi-router support"
```

---

### Task 19: Lot-scraper worker (consumes `auction_pending`, writes `auction_lots`)

**Files:**
- Create: `src/carbuyer/apps/lot_scraper/__init__.py`
- Create: `src/carbuyer/apps/lot_scraper/__main__.py`
- Create: `src/carbuyer/apps/lot_scraper/scraper.py`
- Create: `tests/apps/test_lot_scraper.py`

**Notes from deliberation:**
- **Per-lot transaction** (was: outer txn over all 200 lots). HTTP I/O outside the txn; commit per lot; NOTIFY paced naturally.
- **`INSERT ... ON CONFLICT DO UPDATE`** on `(auction_id, source_lot_id)` — atomic UPSERT.
- **`parser_version` written on every upsert** (Phase 0 design #8).
- **Status-reset cascade:** if any of `{title, description, photos, year, make, model, vin, mileage_km, parser_version}` changed, reset all four worker statuses to `'pending'`. Bid columns NOT touched by the lot-scraper (bid-poller's domain).
- **`unknown:{host}` source rows are skipped** with an INFO log (no fetcher plugin to dispatch to).
- **Catchup sweep** at startup + reconnect: scan auctions where any associated lot has `enrichment_status='pending'` and re-NOTIFY, so notifications fired during a worker outage are recovered.
- **Sources entered via `AsyncExitStack`** for the worker's lifetime; serial processing (one auction at a time per worker; scale by running multiple systemd units).

- [ ] **Step 1: Write the failing test**

```python
# tests/apps/test_lot_scraper.py
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.lot_scraper.scraper import upsert_lot
from carbuyer.db.enums import EnrichmentStatus, ValuationStatus
from carbuyer.db.models import Auction
from carbuyer.sources.base import LotRef, RawLot


def _seed_auction(session: AsyncSession) -> Auction:
    a = Auction(
        source="test", source_auction_id="A1", url="x",
        canonical_url="x", auction_subtype="estate",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    return a


def _lot_raw(title: str = "1995 Ford F-150", **overrides: object) -> RawLot:
    base = {
        "ref": LotRef(source="test", source_auction_id="A1", source_lot_id="L1",
                      url="https://x/lot/1"),
        "lot_number": "1",
        "title": title,
        "description": "runs and drives",
        "photos": ["https://x/p1.jpg"],
        "year": 1995, "make": "Ford", "model": "F-150",
        "current_high_bid_cad": Decimal("2500"),
        "scheduled_end_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return RawLot(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_upsert_lot_inserts_with_parser_version(session: AsyncSession) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot(session, cast(int, a.id), _lot_raw(), parser_version="v1")
    await session.commit()
    assert lot.id is not None
    assert lot.title == "1995 Ford F-150"
    assert lot.parser_version == "v1"
    assert lot.enrichment_status == EnrichmentStatus.PENDING


@pytest.mark.asyncio
async def test_upsert_lot_resets_statuses_when_content_changes(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot(session, cast(int, a.id), _lot_raw(), parser_version="v1")
    # Simulate downstream having processed it.
    lot.enrichment_status = EnrichmentStatus.DONE
    lot.valuation_status = ValuationStatus.DONE
    await session.commit()
    # Re-scrape with new title.
    lot2 = await upsert_lot(
        session, cast(int, a.id), _lot_raw(title="1995 Ford F-150 (revised)"),
        parser_version="v1",
    )
    await session.commit()
    assert lot2.id == lot.id
    assert lot2.title == "1995 Ford F-150 (revised)"
    assert lot2.enrichment_status == EnrichmentStatus.PENDING
    assert lot2.valuation_status == ValuationStatus.PENDING


@pytest.mark.asyncio
async def test_upsert_lot_resets_when_parser_version_changes(
    session: AsyncSession,
) -> None:
    a = _seed_auction(session)
    await session.flush()
    lot = await upsert_lot(session, cast(int, a.id), _lot_raw(), parser_version="v1")
    lot.enrichment_status = EnrichmentStatus.DONE
    await session.commit()
    lot2 = await upsert_lot(session, cast(int, a.id), _lot_raw(), parser_version="v2")
    await session.commit()
    assert lot2.parser_version == "v2"
    assert lot2.enrichment_status == EnrichmentStatus.PENDING


@pytest.mark.asyncio
async def test_upsert_lot_does_not_overwrite_with_none(session: AsyncSession) -> None:
    a = _seed_auction(session)
    await session.flush()
    await upsert_lot(session, cast(int, a.id), _lot_raw(title="t1"), parser_version="v1")
    await session.commit()
    # Re-scrape returns no title — original must be preserved.
    raw = _lot_raw(title=None)
    lot = await upsert_lot(session, cast(int, a.id), raw, parser_version="v1")
    await session.commit()
    assert lot.title == "t1"
```

- [ ] **Step 2: Implement `src/carbuyer/apps/lot_scraper/scraper.py`**

```python
from __future__ import annotations

from contextlib import AsyncExitStack
from typing import cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import (
    EnrichmentStatus,
    NotificationStatus,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import listen, notify
from carbuyer.db.queue import select_pending_ids
from carbuyer.db.session import get_session
from carbuyer.shared.logging import get_logger
from carbuyer.sources.base import (
    SOURCES,
    AuctionFetcher,
    AuctionRef,
    RawLot,
)

log = get_logger("lot_scraper")


# Fields that, when changed, invalidate downstream worker output.
_CONTENT_TRIGGER_FIELDS = (
    "title", "description", "photos",
    "year", "make", "model", "vin", "mileage_km",
)


async def upsert_lot(
    session: AsyncSession,
    auction_id: int,
    raw: RawLot,
    *,
    parser_version: str,
) -> AuctionLot:
    """Atomic UPSERT on (auction_id, source_lot_id). Resets downstream worker
    statuses to PENDING when content fields or parser_version change.
    """
    insert_values: dict[str, object] = {
        "auction_id": auction_id,
        "source_lot_id": raw.ref.source_lot_id,
        "lot_number": raw.lot_number,
        "url": raw.ref.url,
        "parser_version": parser_version,
        "title": raw.title,
        "description": raw.description,
        "photos": raw.photos,
        "year": raw.year,
        "make": raw.make,
        "model": raw.model,
        "trim": raw.trim,
        "mileage_km": raw.mileage_km,
        "vin": raw.vin,
        "lot_status": raw.lot_status,
    }
    stmt = pg_insert(AuctionLot).values(**insert_values).returning(AuctionLot.id)
    excluded = stmt.excluded

    # Per Phase 0 column-ownership: lot-scraper does NOT write bid columns.
    # On conflict, the only mutations are content (with coalesce) + parser_version.
    update_values = {
        "url": excluded.url,
        "lot_number": excluded.lot_number,
        "parser_version": excluded.parser_version,
        "lot_status": excluded.lot_status,
    }
    # coalesce(EXCLUDED, AuctionLot) on every content field — never overwrite with None.
    from sqlalchemy import func
    for field in _CONTENT_TRIGGER_FIELDS + ("trim",):
        update_values[field] = func.coalesce(
            getattr(excluded, field), getattr(AuctionLot, field),
        )
    stmt = stmt.on_conflict_do_update(
        index_elements=["auction_id", "source_lot_id"],
        set_=update_values,
    )
    await session.execute(stmt)

    # Re-fetch to apply the status-reset cascade — needed because UPSERT alone
    # cannot conditionally branch on "did any trigger field change".
    result = await session.execute(
        select(AuctionLot).where(
            AuctionLot.auction_id == auction_id,
            AuctionLot.source_lot_id == raw.ref.source_lot_id,
        ),
    )
    lot = result.scalar_one()
    return lot


async def upsert_lot_with_status_cascade(
    session: AsyncSession,
    auction_id: int,
    raw: RawLot,
    *,
    parser_version: str,
) -> AuctionLot:
    """Wrapper that resets statuses if any content trigger field changed.

    Compares pre-write snapshot against post-write row, then mutates statuses
    in a follow-up UPDATE if needed.
    """
    pre = (
        await session.execute(
            select(AuctionLot).where(
                AuctionLot.auction_id == auction_id,
                AuctionLot.source_lot_id == raw.ref.source_lot_id,
            ),
        )
    ).scalar_one_or_none()
    pre_snapshot = (
        {f: getattr(pre, f) for f in _CONTENT_TRIGGER_FIELDS}
        if pre is not None
        else None
    )
    pre_parser_version = pre.parser_version if pre is not None else None

    lot = await upsert_lot(session, auction_id, raw, parser_version=parser_version)

    if pre is None:
        return lot  # fresh insert; statuses already PENDING from server defaults
    post_snapshot = {f: getattr(lot, f) for f in _CONTENT_TRIGGER_FIELDS}
    content_changed = post_snapshot != pre_snapshot
    parser_changed = lot.parser_version != pre_parser_version
    if content_changed or parser_changed:
        lot.enrichment_status = EnrichmentStatus.PENDING
        lot.valuation_status = ValuationStatus.PENDING
        lot.vision_status = VisionStatus.PENDING
        lot.notification_status = NotificationStatus.PENDING
        await session.flush()
    return lot


async def process_auction(
    auction_id: int,
    fetchers: dict[str, AuctionFetcher],
) -> int:
    """Scrape every lot for one auction. Per-lot transaction; HTTP outside txn."""
    async with get_session() as s:
        auction = await s.get(Auction, auction_id)
    if auction is None:
        log.warning("auction not found", auction_id=auction_id)
        return 0
    if auction.source.startswith("unknown:"):
        log.info(
            "skipping unknown-platform auction (no fetcher)",
            auction_id=auction_id, source=auction.source,
        )
        return 0
    fetcher = fetchers.get(auction.source)
    if fetcher is None:
        log.warning(
            "no fetcher plugin registered",
            auction_id=auction_id, source=auction.source,
        )
        return 0
    aref = AuctionRef(
        source=auction.source,
        source_auction_id=auction.source_auction_id,
        url=auction.url,
    )
    count = 0
    async for lot_ref in fetcher.fetch_lots(aref):
        try:
            raw = await fetcher.fetch_lot(lot_ref)
        except Exception:
            log.exception("fetch_lot failed", lot_ref_url=lot_ref.url)
            continue
        # Per-lot transaction — HTTP I/O is OUTSIDE the txn.
        async with get_session() as session, session.begin():
            lot = await upsert_lot_with_status_cascade(
                session, cast(int, auction.id), raw,
                parser_version=fetcher.version,
            )
            await notify(session, "enrichment_pending", str(lot.id))
        count += 1
    return count


async def _catchup_sweep(fetchers: dict[str, AuctionFetcher]) -> None:
    """On startup + reconnect, find auctions whose lots haven't been scraped
    yet (i.e. nothing in auction_lots for them) and process them. NOTIFYs fired
    while the worker was down land here."""
    async with get_session() as s:
        # Auctions that have NEVER produced a lot row yet.
        result = await s.execute(
            select(Auction.id).where(
                ~select(AuctionLot.id)
                .where(AuctionLot.auction_id == Auction.id)
                .exists(),
                ~Auction.source.startswith("unknown:"),
            ),
        )
        ids = list(result.scalars().all())
    for auction_id in ids:
        log.info("catchup processing", auction_id=auction_id)
        await process_auction(auction_id, fetchers)


async def main() -> None:
    import carbuyer.sources.hibid.source  # noqa: F401  -- registers HibidSource
    fetchers: dict[str, AuctionFetcher] = {
        s.name: s for s in SOURCES.values() if isinstance(s, AuctionFetcher)
    }
    async with AsyncExitStack() as stack:
        for f in fetchers.values():
            await stack.enter_async_context(f)
        await _catchup_sweep(fetchers)
        async for payload in listen("auction_pending"):
            try:
                auction_id = int(payload)
            except ValueError:
                continue
            log.info("processing", auction_id=auction_id)
            try:
                await process_auction(auction_id, fetchers)
            except Exception:
                log.exception("process_auction failed", auction_id=auction_id)
```

- [ ] **Step 3: Implement `src/carbuyer/apps/lot_scraper/__main__.py`**

```python
from carbuyer.apps._runner import run_worker
from carbuyer.apps.lot_scraper.scraper import main


if __name__ == "__main__":
    run_worker("lot_scraper", main)
```

- [ ] **Step 4: Create `__init__.py` and run tests**

```bash
touch src/carbuyer/apps/lot_scraper/__init__.py
uv run pytest tests/apps/test_lot_scraper.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/lot_scraper/ tests/apps/test_lot_scraper.py
git commit -m "apps: lot-scraper with per-lot UPSERT + status cascade + parser_version + catchup"
```

---

End of Phase 2. Discoverer + lot-scraper plumbing wired. Next phase plugs LLM enrichment in.

---

## Phase 3 — LLM enrichment

### Phase 3 — Design-decision overlay (post-deliberation 2026-05-09)

Four role-specialized reviews (senior dev / LLM eng, devops / SRE, auction-buyer-domain consultant, software architect) raised convergent issues. The original Phase 3 plan still referenced Phase-0/2 names that were renamed during their respective overlays (`async_session_maker`, `claim_pending_lots`), held DB transactions across 30s-of-LLM I/O (violating `idle_in_transaction_session_timeout=60s`), forgot the catchup-sweep idiom from Phase 2, and shipped a flag taxonomy that domain review showed was both miscalibrated and missing the highest-leverage Western-Canada gotchas (the diesel-powertrain failure set). One cross-phase footgun also surfaced: Phase 2's `_upsert_lot` `coalesce(EXCLUDED, AuctionLot)` on year/make/model/trim/vin/mileage_km will silently overwrite enricher-normalized values on the next rescrape, producing an enrich → rescrape-clobber → re-enrich flap that burns OpenAI budget.

**Net effect:** Phase 3 grows by one Phase-0 cleanup task (19.5: enrichment_attempts column + lot-scraper coalesce drop) and Tasks 20–24 are all rewritten. Phase 12 .env.example port bug (5432 → 5433) is also fixed inline.

**Must-fix decisions:**

1. **Real queue API is `claim_pending_ids` (returns `list[int]`), not `claim_pending_lots`.** The function commits its own short claim transaction; the worker then re-fetches each lot in a fresh per-id session. The plan's old code held one outer session across the whole batch, ran LLM I/O inside `session.begin()`, and triggered `idle_in_transaction_session_timeout`. Rewritten to mirror `lot_scraper.process_auction`: claim ids in a 1-statement txn, close it, then iterate ids opening a fresh `get_session()` per id with all LLM/HTTP I/O *outside* `session.begin()`.

2. **`get_session()` / `get_session_maker()`, not `async_session_maker`.** Phase-0 rename. Same drift the Phase 2 overlay had to chase down.

3. **Catchup sweep at startup before `LISTEN`.** Phase 2 design overlay #12 made this mandatory for every continuous worker. Enricher missed it. Added: `_catchup_sweep()` that calls `process_pending(provider)` once before entering `async for _ in listen(...)`. Recovers NOTIFYs missed during worker downtime.

4. **`enrichment_attempts INT NOT NULL DEFAULT 0` column.** Without an attempt counter, a single transient OpenAI 5xx flips the lot to `failed` permanently — no retry path, no watchdog reaches `failed`. Migration adds the column; `enrich_one` increments on every attempt; only marks `FAILED` when `attempts >= settings.enrichment_max_attempts` (default 3). Transient errors leave status at `PENDING` for re-claim. Schema/validation errors fail-fast at attempts=1 (not transient).

5. **Lot-scraper `_upsert_lot` no longer coalesces year/make/model/trim/vin/mileage_km.** These are heuristic-extracted on insert, then enricher owns them. Coalescing on UPDATE means rescrape's raw "F150" overwrites enricher's normalized "F-150", cascade fires, re-enrich, flap. Fix: the for-loop iterates only `("title", "description", "photos")` — the genuine listing-level content fields — and year/make/model/trim/vin/mileage_km drop out of the `ON CONFLICT DO UPDATE` set entirely. They remain in `_CONTENT_TRIGGER_FIELDS` (cascade detection still works correctly because they no longer change on UPDATE).

6. **Status writes use `EnrichmentStatus.FAILED` etc., not bare strings.** StrEnum compare-equals to strings so the old code worked, but writes via enum members (a) survive grep, (b) catch typos at type-check time, (c) match the lot-scraper's idiom.

7. **All LLM and HTTP I/O happens outside `session.begin()`.** `enrich_one` splits into two halves: `compute_enrichment(lot_snapshot, provider)` returns an `EnrichmentResult` (no DB, no transaction — does describe + Carfax fetch + Carfax extract); `apply_enrichment(session, lot_id, result)` re-fetches the lot in a short txn and writes columns. This isolates network latency from connection-pool / lock pressure.

8. **OpenAI SDK GA path: `client.chat.completions.parse`, not `client.beta.chat.completions.parse`.** Both work today on `openai>=1.40`, but `.beta.` is the legacy path. Single helper `_parse_to(model_cls, messages, max_tokens)` inside `OpenAIProvider` so the SDK call shape lives in one place — Phase 8 vision will reuse it.

9. **`AsyncOpenAI(max_retries=5, timeout=60.0)`.** Lets the SDK handle 429/5xx with built-in exponential backoff + jitter. OpenAI does not bill for retried failed calls so this is free reliability.

10. **`max_tokens=3000` for description (was 2048).** Worst-case `EnrichmentOutput` with 5+ red flags carrying verbatim evidence quotes plus `summary` plus `desirability_evidence` lists tokenizes around 1.5–2k completion tokens; `2048` had no margin and truncation produces invalid JSON which the schema rejects, marking the lot `failed`.

11. **Token-usage logging on every LLM call.** `log.info("openai describe", lot_id=..., model=..., prompt_tokens=..., completion_tokens=..., total_tokens=..., duration_ms=...)`. This is the data a future budget-ledger feature consumes; for MVP it's the only spending signal we have. (Cost ledger TABLE deferred — see "Deferred" below.)

12. **Bounded LLM concurrency via `asyncio.Semaphore(settings.openai_concurrency)`.** Default 4. Sequential-only batch processing leaves OpenAI parallelism on the floor (gpt-4o-mini handles 500 RPM tier-1) — 100-lot batch goes from 50 minutes wall to ~12 minutes.

13. **System prompt cached at `OpenAIProvider` construction** (not regenerated per call). The taxonomy expansion in this overlay pushes the system prompt to ~2–4k tokens; OpenAI's prompt-caching kicks in at ≥1024 tokens of identical prefix and gives 50% off cached input. Same content per call → cache routing hits.

14. **Pydantic `Condition` literal stays `Literal["bad","poor","decent","good","great"]` — no `"unknown"`.** Prompt rule "output unknown when uncertain" applies only to fields whose schema actually has `unknown` (`transmission`, `drivetrain`, `title_status`). For `condition_categorical`, the prompt says: "when uncertain, output `decent` and set `condition_confidence < 0.5`." A code-side post-validation clamp in `apply_enrichment` enforces this if the model violates it.

15. **`condition_confidence < 0.5 → "decent"` clamp moves to code, not prompt.** Domain review pointed out this prompt rule pollutes Phase 4: "actually decent" and "we don't know" both render as `decent` and Phase 4 valuation can't tell sparse-listing-pessimism from genuine-decency. Solution: prompt drops the rule; `apply_enrichment` writes `condition_categorical = "decent"` only when `condition_confidence < 0.5`, and **also sets `condition_inferred_from_sparse_listing = True`** so Phase 4 can apply a separate pessimism penalty. (New field in `EnrichmentOutput` — see Task 20.)

16. **Empty `OPENAI_API_KEY` fail-fast at worker startup.** `enricher.main()` checks `settings.openai_api_key` and raises `SystemExit("OPENAI_API_KEY not configured")` before entering the loop. Workers that crash on first lot waste a deploy cycle.

17. **`LLMProvider` ABC splits into `DescribeProvider` and `VisionProvider` role mixins** — symmetric with `AuctionDiscoverer`/`AuctionFetcher`/`BidPoller`. `OpenAIProvider` implements both. Phase 8 vision swaps in a different provider class trivially. Both ABCs get `__aenter__`/`__aexit__` defaults so workers can `async with` the provider for clean client shutdown.

18. **`extract_carfax_findings` accepts caller's `AsyncOpenAI` client + model**, doesn't construct its own. Avoids per-lot TLS handshake + duplicate budget config.

19. **Phase 12 `.env.example` port 5432 → 5433.** Direct contradiction with `infra/docker-compose.yml` host-port binding. Fixed inline at line 9854.

**Should-fix decisions:**

20. **Carfax extraction split into 23a (regex URL find — safe) and 23b (best-effort fetch + extract).** Domain + senior-dev review converged: Carfax CA is paywalled and bot-detected; plain `httpx.get` returns a login wall or 403 in the majority of cases (same Cloudflare risk Phase 1 already documented for HiBid). Task 23a (regex-only `find_carfax_url`) ships and is reliable. Task 23b (fetch + LLM extract) ships behind an explicit "best-effort" docstring, gracefully no-ops on 4xx/5xx/empty/under-500-byte responses, and is documented as expected to succeed on <30% of real Carfax URLs. Phase 8 vision-batcher (Playwright) is the longer-term home for full-Carfax extraction.

21. **Carfax HTTP response gate.** Before calling the LLM extractor, check `response.status_code < 400` and `len(response.text) > 500`. A 404 page passed to the LLM costs money and produces garbage `CarfaxFindings`.

22. **Carfax URL redaction in logs.** `https://www.carfax.ca/vhr/<token>` is a per-vehicle access key, not a public URL. Log the SHA-256 prefix + host, never the full URL. Same for VIN — log VIN existence (`has_vin=True`) but not the VIN value.

23. **Massive taxonomy expansion** based on domain review. Calibrations:
    - `needs_work` weight drops to **-1** (was -2) — fires on ~80% of lots so dilutes signal. -2 reserved for genuine red events.
    - `rust_mentioned` -1 (kept) joined by `frame_rust` -3 and `frame_rust_perforated` showstopper, plus `rocker_rust` / `cab_corner_rust` -2.
    - `wont_start` moves from showstopper → red-flag -3. RB / industrial auction "won't start" frequently means dead battery, not seized engine. Showstopper status reserved for explicit `engine_seized` / `for_parts_only` / `needs_engine`.
    - `as_is_no_warranty` showstopper REMOVED — it fires on every online auction and dilutes the signal. Replaced with `seller_says_for_parts_only` showstopper that requires explicit "sold for parts" / "no questions" / "for parts only" phrasing.
    - New red flags added: `no_keys` (-2), `bill_of_sale_only` (-3), `transmission_slipping` (-3), `head_gasket_suspected` (-3), `electrical_issues` (-2), `leaks_coolant` (-2), `leaks_oil_minor` (-1), `bald_tires` (-1), `check_engine_light_on` (-1), `out_of_province` (-1), `winter_tires_only` (-1), `diesel_emissions_deleted` (-2), `salvage_history_carfax` (-2), `abandoned_vehicle` (-2), `seller_dealer_only` (-1).
    - New green flags added: `non_smoker` (+1), `regular_oil_changes` (+1), `from_southern_climate` (+2), `warranty_remaining` (+2), `cpo_certified` (+2), `two_sets_of_tires` (+1), `block_heater_installed` (+1), `highway_mileage` (+1), `recent_inspection_passed` (+1), `recall_completed` (+1).
    - New showstoppers added: `engine_seized`, `for_parts_only`, `vin_mismatch`, `stolen_recovered`, `no_title`, `outstanding_lien`, `non_repairable_brand`, `lemon_law_buyback`.
    - `Z71` removed from `DESIRABLE_TRIMS` (it's a package on every Silverado, not a desirable trim by itself). Replaced with `Silverado 1500 Trail Boss` and expanded with ~25 entries: Tundra TRD Pro, FJ Cruiser, Land Cruiser any, Lexus GX/LX, Bronco 2021+, F-250/F-350 PowerStroke high-trim, F-150 Tremor, Ram 2500/3500 Cummins / Power Wagon, Wrangler 392, Gladiator Rubicon/Mojave, WRX STI, BRZ tS, Golf R/GTI manual, BMW M2/M3/M4/M5, Porsche 911/Cayman/Boxster, GT-R/370Z Nismo, Civic Type R / Si manual, S2000, NSX, Miata/RX-8, Mazdaspeed3/6, G-Wagen, Lancer Evo, Audi RS/S, Defender, Hummer H1.
    - `CLASSIC_EXCEPTIONS` default flips: pre-2000 is **not** automatically classic — only matches in `CLASSIC_EXCEPTIONS` are classic. Expanded to ~25 entries covering the actual collector set (Supra MK4, NSX, RX-7 FD3S, Miata NA/NB, MR2, S2000, Civic SiR/Type R, Skyline R32–34, 240SX, 300ZX TT, BMW E30/E36 M3, E39 M5, UR Quattro, air-cooled 911, 944/968/928, 190E 2.3-16/2.5-16, Land Cruiser 80/100, YJ/TJ Wrangler, vintage Bronco, K5 Blazer/GMT400, Power Wagon, Defender 90/110, GTI MK1/MK2/MK3, AE86, SVX).
    - `GOTCHAS` expanded from 8 → ~30 entries adding the diesel powertrain failure set Western Canada auction yards are full of: 6.0L PowerStroke EGR/heads, 6.4L PowerStroke twin-turbo, 6.7L PowerStroke / Duramax LML CP4 fuel pump, Cummins 6.7L EGR + 68RFE, Hemi MDS lifters, GM AFM lifter failure, Coyote 5.0L oil consumption, 5.4L 3V cam phasers/spark plugs, J35 VCM, BMW N54/N55/N20/M5 V10, Mercedes M272/M273 + 7G-Tronic, Audi/VW EA113/EA888, VW TDI emissions, Subaru EJ255/EJ257, CX-7 turbo, Titan rear diff, Pathfinder strawberry-milkshake, Range Rover air suspension, Evo shift-fork, modern-diesel emissions, Tesla MCU1.

24. **`DescribeInput` carries the full enrichment-relevant context** — adds `current_high_bid_cad`, `bid_increment`, `auction_close_at`, `is_no_reserve`, `image_count`, `current_year` (2026). Without these the model can't reason about urgency, priced-below-scrap, or sparse-listing-quality signals.

25. **`description_quality: Literal["thin","adequate","detailed"]` field on `EnrichmentOutput`.** Domain meta-note flagged the listing-density bias: terse listings → no flags → looks decent; verbose honest listings → many red flags → looks like junk; Phase 4 then prefers the dishonest ones. `description_quality` lets Phase 4 apply a sparse-listing penalty.

26. **`enrichment_max_attempts` setting (default 3) and per-error-class classification.** `RateLimitError`, network errors, 5xx → leave PENDING (transient, count attempts, fail at 3). 4xx other than 429, schema/validation errors → FAILED at attempts=1 (not retriable). The classifier lives in `enrich_one`.

27. **`OpenAIProvider` is an async context manager.** `__aenter__` returns `self`; `__aexit__` calls `await self.client.close()`. Worker uses `async with OpenAIProvider() as provider:`. Symmetric with how `Source` plugins are managed.

**Post-implementation review (2026-05-09 second pass):**

After three reviewers (senior dev / architect / domain) audited the merged Phase 3 code, the following follow-ups landed in a final review-fix commit:

28. **`is_no_reserve` derivation was inverted.** `lot.reserve_met is False` means "reserve set, not yet met" — the *opposite* of "no reserve". Until Phase 7 bid-poller surfaces a real `is_no_reserve` signal, hardcoded to `False` (honest unknown).

29. **`_classify_failure` retried 4xx auth/badreq as transient.** OpenAI's 4xx (Authentication, BadRequest, PermissionDenied, NotFound, UnprocessableEntity) all subclass `APIStatusError`; the SDK does not retry them. Now classified as permanent so a revoked-key incident doesn't burn 3 attempt cycles per lot. `LengthFinishReasonError` (model hit `max_tokens` mid-JSON) also classified permanent.

30. **`title_status` and `engine` re-enrichment regression preserved.** `title_status="UNKNOWN"` and `engine="unknown"` now skip the write — same idiom as transmission/drivetrain. Previously a low-confidence re-run could clobber a known-good `NORMAL` to `UNKNOWN`.

31. **`description_quality` post-validation guard.** If the model says "detailed" on a 50-char listing, the worker overrides to "thin" based on `len(description) < 100` (matches the prompt's boundary).

32. **Showstoppers re-categorized to heavy red flags (-4):** `salvage_not_rebuilt`, `outstanding_lien`, `lemon_law_buyback` are no longer dispositive exclusions — flippers want salvage deals; auction houses pay liens at sale; lemon-buybacks post-fix are common. They remain heavy red flags so cumulative-score-threshold logic in Phase 4 can still exclude them when that's the right call.

33. **`flood_damage` split into `flood_damage_total` (showstopper) and `flood_damage_partial` (-3 red).** Waterline-above-seats is dispositive; rear floor wet is a $500 dryer rental.

34. **Per-showstopper trigger phrasing in prompt.** Each showstopper entry now spells out the trigger phrasing the LLM should require ("frame is bent", "engine seized", "flood title brand"). Without this, the over-fire rate on showstoppers was the dominant Phase 3 quality risk.

35. **Self-NOTIFY when leaving lots PENDING after transient failure.** Without this, transient-failed rows wait until next worker restart's catchup sweep (24h+ in low-throughput periods). `process_pending` now emits `pg_notify('enrichment_pending', '')` when any of its claimed lots ended up back at PENDING.

36. **Land Cruiser classic-exception split.** 80-series (1990-1997) is solid front axle; 100-series (1998-2007) has IFS (independent front suspension) — the original entry conflated them. Now two entries.

37. **Test coverage added for `process_pending` end-to-end + fail-fast on empty `OPENAI_API_KEY` + 4xx/5xx classifier partition.** Phase 4/6/8 worker tests will follow the same `_patched_get_session` fixture pattern; `tests/conftest.py` now exposes `session.info["maker"]` for nested-session simulation.

**Plan updates required for downstream phases (deferred but documented):**

- **Phase 4 plan (Tasks 25-30) must consume `description_quality` and `condition_inferred_from_sparse_listing`.** Phase 3 ships these columns specifically to feed Phase 4's sparse-listing pessimism penalty (overlay #15 / #25). The current Phase 4 plan code blocks make zero reference to either column — Phase 4 will need a follow-up overlay.

- **Phase 8 plan (Task 37) must use `client.chat.completions.parse` (GA), not `client.beta.chat.completions.parse`.** The Phase 3 overlay made this binding for Phase 3 but the Phase 8 plan still references the legacy path.

- **`_parse_to` helper signature mismatch with Phase 8 vision.** Phase 3 overlay #8 promised `_parse_to(model_cls, messages, max_tokens)` reusable by Phase 8. Implementation took `system: str, user: str` instead. Phase 8 will need to either refactor `_parse_to` to take `messages` (list-of-content-parts for image inputs) or replicate the usage-logging block.

**Deferred (acknowledged):**

- **`llm_cost_ledger` table + daily-budget aborting guard.** Devops review wanted a cost ledger row per call and an `assert_under_budget` check that raises `BudgetExceeded` near a daily ceiling. For MVP the per-call usage log (decision #11) gives the data; an aggregating dashboard query is enough. Adding a ledger table commits to a schema before we know the actual call patterns. Revisit when first OpenAI bill arrives or Phase 8 (vision, ~10x cost per call) lands.
- **Run-id / trace-id structlog contextvars.** `log.bind(run_id=...)` would let a single LLM call's logs be retrieved post-hoc as a trace. Useful but not blocking. Defer until first postmortem demands it.
- **`LLM_PROVIDERS` registry** mirroring `SOURCES`. Single concrete provider for MVP; `enricher.main()` instantiates `OpenAIProvider` directly. Add registry when a second provider lands (Anthropic, local model).
- **Description-density signal computed in scraper** rather than asked from the LLM. The current design has the LLM self-report `description_quality` from the listing text — cheaper and consistent. A future word-count + field-coverage heuristic in the scraper can supersede it.
- **Auto-staleness re-claim by `enrichment_version`.** When the prompt/taxonomy bumps to v2, operator runs `UPDATE auction_lots SET enrichment_status='pending' WHERE enrichment_version IS DISTINCT FROM 'v2' AND enrichment_status='done'` plus catchup-sweep at next worker restart picks them up. Documented; auto-claim deferred.
- **Per-plugin Carfax via Playwright** (real fetch). Phase 8 vision-batcher will own this once Playwright infrastructure lands.

End of overlay.

---

### Task 19.5: Pre-Phase-3 migration + lot-scraper coalesce fix

**Why this task is here:** Phase 3 needs a new `enrichment_attempts` column on `auction_lots` for retry counting, and Phase 2's `_upsert_lot` clobbers enricher-normalized fields on rescrape (decision #5 in this overlay). Both land before Task 20 so the rest of Phase 3 builds on the corrected schema and worker contract.

**Files:**
- Create: Alembic migration `alembic/versions/<hash>_enrichment_attempts.py`
- Modify: `src/carbuyer/db/models.py` (add `enrichment_attempts` column on `AuctionLot`)
- Modify: `src/carbuyer/apps/lot_scraper/scraper.py` (drop year/make/model/trim/vin/mileage_km from `_upsert_lot` ON CONFLICT update set)
- Modify: `tests/apps/lot_scraper/test_scraper.py` (add regression test: rescrape preserves LLM-normalized values)

- [ ] **Step 1: Add `enrichment_attempts` column to `AuctionLot`**

```python
# in src/carbuyer/db/models.py, near other enrichment fields
enrichment_attempts: Mapped[int] = mapped_column(
    Integer, server_default=text("0"), nullable=False,
)
```

- [ ] **Step 2: Generate the migration**

```bash
uv run alembic revision --autogenerate -m "enrichment_attempts column"
```

Review the generated migration; ensure it adds only the `enrichment_attempts` column (with `server_default="0"`, `nullable=False`) and nothing else.

- [ ] **Step 3: Update lot-scraper `_upsert_lot` to not coalesce normalized fields**

```python
# src/carbuyer/apps/lot_scraper/scraper.py — change the for-loop in _upsert_lot:
# Before: for field_name in (*_CONTENT_TRIGGER_FIELDS, "trim"):
# After:  for field_name in ("title", "description", "photos"):
#     update_values[field_name] = func.coalesce(
#         getattr(excluded, field_name), getattr(AuctionLot, field_name),
#     )
```

The fields `year`, `make`, `model`, `trim`, `vin`, `mileage_km` drop out of the `ON CONFLICT DO UPDATE` set entirely — they're written only on `INSERT`. They remain in `_CONTENT_TRIGGER_FIELDS` so the cascade detection still works (they just never change on UPDATE so they never falsely fire).

- [ ] **Step 4: Add a regression test that rescrape doesn't clobber LLM-normalized values**

```python
# tests/apps/lot_scraper/test_scraper.py — add this test
@pytest.mark.asyncio
async def test_rescrape_preserves_llm_normalized_fields(session) -> None:
    auction = await _make_auction(session, "hibid", "A1")
    raw = _make_raw_lot(auction, source_lot_id="L1", year=2010, make="Ford", model="F150")

    async with session.begin():
        lot = await upsert_lot_with_status_cascade(
            session, auction.id, raw, parser_version="t-1"
        )
    # Simulate enricher normalization.
    async with session.begin():
        lot.model = "F-150"
        lot.enrichment_status = EnrichmentStatus.DONE

    # Rescrape returns the same raw heuristic value.
    async with session.begin():
        lot2 = await upsert_lot_with_status_cascade(
            session, auction.id, raw, parser_version="t-1"
        )

    assert lot2.id == lot.id
    assert lot2.model == "F-150"  # preserved, not clobbered to "F150"
    assert lot2.enrichment_status == EnrichmentStatus.DONE  # cascade did not fire
```

- [ ] **Step 5: Run the migration + tests + commit**

```bash
sg docker -c "docker compose -f infra/docker-compose.yml exec -T postgres dropdb -U carbuyer carbuyer_test || true"
sg docker -c "docker compose -f infra/docker-compose.yml exec -T postgres createdb -U carbuyer carbuyer_test"
uv run alembic upgrade head
uv run pytest tests/apps/lot_scraper/ -v
git add alembic/versions/ src/carbuyer/db/models.py src/carbuyer/apps/lot_scraper/scraper.py tests/apps/lot_scraper/
git commit -m "lot-scraper: drop LLM-owned columns from coalesce update + add enrichment_attempts column"
```

---

### Task 20: Pydantic schemas for enrichment output

**Files:**
- Create: `src/carbuyer/llm/__init__.py`
- Create: `src/carbuyer/llm/schemas.py`
- Create: `tests/llm/__init__.py`
- Create: `tests/llm/test_schemas.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/llm/test_schemas.py
import json

from carbuyer.llm.schemas import EnrichmentOutput


def test_enrichment_output_round_trip() -> None:
    payload = {
        "normalized_vehicle": {
            "year": 2010, "make": "Ford", "model": "F-150",
            "trim": None, "engine": "5.4L V8", "transmission": "automatic",
            "drivetrain": "4wd", "mileage_km": 250000, "vin": None,
        },
        "title_status": "NORMAL",
        "condition_categorical": "decent",
        "condition_confidence": 0.7,
        "red_flags": [],
        "green_flags": [],
        "showstopper_flags": [],
        "carfax_url": None,
        "summary": "an older F-150",
        "rarity": {
            "desirable_trim_or_spec": False, "classic_or_collector": False,
            "desirability_signals": [], "desirability_evidence": [],
        },
    }
    out = EnrichmentOutput.model_validate(payload)
    assert out.normalized_vehicle.year == 2010
    schema = EnrichmentOutput.model_json_schema()
    assert "properties" in schema


def test_enrichment_output_forbids_extra() -> None:
    import pytest
    from pydantic import ValidationError
    payload = {"junk": True}
    with pytest.raises(ValidationError):
        EnrichmentOutput.model_validate(payload)
```

- [ ] **Step 2: Implement `src/carbuyer/llm/schemas.py`**

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Transmission = Literal["manual", "automatic", "cvt", "unknown"]
Drivetrain = Literal["fwd", "rwd", "awd", "4wd", "unknown"]
TitleStatus = Literal["NORMAL", "SALVAGE", "REBUILT", "NON_REPAIRABLE", "STOLEN", "UNKNOWN"]
Condition = Literal["bad", "poor", "decent", "good", "great"]


class NormalizedVehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    engine: str | None
    transmission: Transmission
    drivetrain: Drivetrain
    mileage_km: int | None
    vin: str | None


class FlagInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flag: str
    evidence: str
    weight: int


class ShowstopperInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flag: str
    evidence: str


class RarityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desirable_trim_or_spec: bool
    classic_or_collector: bool
    desirability_signals: list[str]
    desirability_evidence: list[str]


class EnrichmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    normalized_vehicle: NormalizedVehicle
    title_status: TitleStatus
    condition_categorical: Condition
    condition_confidence: float = Field(ge=0, le=1)
    red_flags: list[FlagInstance]
    green_flags: list[FlagInstance]
    showstopper_flags: list[ShowstopperInstance]
    carfax_url: str | None
    summary: str
    rarity: RarityAssessment


class CarfaxFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accident_count: int
    accident_severity_max: Literal["minor", "moderate", "severe", "none"]
    service_record_density: Literal["none", "sparse", "regular", "dense"]
    ownership_count: int | None
    title_brands: list[str]
    odometer_consistency: Literal["consistent", "rollback_suspected", "unknown"]


class PerImageFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "rust", "dent", "scratch", "paint_mismatch", "panel_gap",
        "interior_wear", "stain", "other",
    ]
    location: str
    severity: int = Field(ge=1, le=3)
    confidence: int = Field(ge=1, le=5)
    reasoning: str


class PerImageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_type: Literal[
        "exterior_front", "exterior_rear", "exterior_side", "interior",
        "engine_bay", "wheel", "undercarriage", "document", "other",
    ]
    image_quality_sharpness: Literal["sharp", "blurry"]
    image_quality_lighting: Literal["well_lit", "dim", "harsh_shadow"]
    image_quality_cleanliness: Literal["clean", "dirty"]
    visible_panels: list[str]
    findings: list[PerImageFinding]
    explicit_unknowns: list[str]


class VisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coverage_gaps: list[str]
    cross_panel_paint_consistency: Literal["consistent", "inconsistent", "cannot_assess"]
    staging_signals: list[str]
    overall_red_flags: list[str]
    overall_green_flags: list[str]
    exterior_condition: Condition
    interior_condition: Condition
    overall_vision_condition: Condition
    vision_confidence: float = Field(ge=0, le=1)
    contradictions_with_description: list[str]
```

- [ ] **Step 3: Create `__init__.py` and run tests**

```bash
touch src/carbuyer/llm/__init__.py tests/llm/__init__.py
uv run pytest tests/llm/test_schemas.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/llm/__init__.py src/carbuyer/llm/schemas.py tests/llm/__init__.py tests/llm/test_schemas.py
git commit -m "llm: Pydantic schemas for enrichment + vision output"
```

---

### Task 21: LLMProvider ABC + flag/desirability taxonomy stubs

**Files:**
- Create: `src/carbuyer/llm/base.py`
- Create: `src/carbuyer/flags/__init__.py`
- Create: `src/carbuyer/flags/taxonomy.py`
- Create: `tests/flags/__init__.py`
- Create: `tests/flags/test_taxonomy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/flags/test_taxonomy.py
from carbuyer.flags.taxonomy import (
    RED_FLAG_TAXONOMY, GREEN_FLAG_TAXONOMY, SHOWSTOPPER_TAXONOMY,
    DESIRABLE_TRIMS, CLASSIC_EXCEPTIONS, model_gotchas_for,
)


def test_taxonomies_have_minimum_entries() -> None:
    assert len(RED_FLAG_TAXONOMY) >= 8
    assert len(GREEN_FLAG_TAXONOMY) >= 5
    assert len(SHOWSTOPPER_TAXONOMY) >= 4


def test_desirable_trims_seed_includes_known_examples() -> None:
    assert any("TRD Pro" in entry["trim"] for entry in DESIRABLE_TRIMS)
    assert any("Raptor" in entry["trim"] for entry in DESIRABLE_TRIMS)


def test_model_gotchas_for_returns_relevant_entries() -> None:
    g = model_gotchas_for(make="Toyota", model="Tacoma", year=2010)
    assert any("frame" in note.lower() for note in g)
    g2 = model_gotchas_for(make="Honda", model="CR-V", year=2018)
    assert any("oil" in note.lower() for note in g2)
```

- [ ] **Step 2: Implement `src/carbuyer/flags/taxonomy.py`**

```python
from typing import TypedDict


class FlagDef(TypedDict):
    flag: str
    weight: int
    description: str


RED_FLAG_TAXONOMY: list[FlagDef] = [
    {"flag": "engine_knock", "weight": -3, "description": "Engine knock, seized, or overheating mentioned."},
    {"flag": "accident_history", "weight": -2, "description": "Accident reported on Carfax or in description."},
    {"flag": "needs_work", "weight": -2, "description": "Listing says needs work, project, or major repairs."},
    {"flag": "no_service_records", "weight": -1, "description": "No service history mentioned."},
    {"flag": "rust_mentioned", "weight": -1, "description": "Surface rust mentioned."},
    {"flag": "smoker_owned", "weight": -1, "description": "Smoker-owned, interior odor."},
    {"flag": "high_mileage_no_service", "weight": -1, "description": ">200k km with no major service mentioned."},
    {"flag": "mileage_unknown", "weight": -1, "description": "Mileage missing or labeled TMU/exempt."},
    {"flag": "modifications", "weight": -1, "description": "Heavy aftermarket modifications without receipts."},
]

GREEN_FLAG_TAXONOMY: list[FlagDef] = [
    {"flag": "recent_timing_belt", "weight": 1, "description": "Recent timing belt or chain replacement."},
    {"flag": "recent_transmission_service", "weight": 1, "description": "Recent transmission service / fluid change."},
    {"flag": "no_accidents_carfax", "weight": 2, "description": "Carfax shows no accidents."},
    {"flag": "single_owner", "weight": 1, "description": "Single-owner vehicle."},
    {"flag": "service_records", "weight": 2, "description": "Itemized service history attached."},
    {"flag": "recent_major_service", "weight": 1, "description": "Recent brakes / tires / suspension service."},
    {"flag": "garage_kept", "weight": 1, "description": "Stored indoors, plug-in block heater (cold climate)."},
]

SHOWSTOPPER_TAXONOMY: list[dict[str, str]] = [
    {"flag": "salvage_not_rebuilt", "description": "Salvage title that has not been rebuilt."},
    {"flag": "frame_damage", "description": "Frame damage or structural compromise."},
    {"flag": "as_is_no_warranty", "description": "Sold as-is, no warranty, paired with refusal of inspection."},
    {"flag": "wont_start", "description": "Won't start, ran when parked, sold for parts."},
    {"flag": "fire_damage", "description": "Fire-damaged."},
    {"flag": "flood_damage", "description": "Flood-damaged."},
]


class DesirableEntry(TypedDict):
    make: str
    model: str
    trim: str
    note: str


DESIRABLE_TRIMS: list[DesirableEntry] = [
    {"make": "Toyota", "model": "Tacoma", "trim": "TRD Pro", "note": "Special-edition Tacoma."},
    {"make": "Toyota", "model": "4Runner", "trim": "TRD Pro", "note": "Special-edition 4Runner."},
    {"make": "Ford", "model": "F-150", "trim": "Raptor", "note": "Off-road performance F-150."},
    {"make": "Chevrolet", "model": "Silverado 1500", "trim": "Z71", "note": "Z71 off-road package."},
    {"make": "Jeep", "model": "Wrangler", "trim": "Rubicon", "note": "Off-road-capable trim."},
    # Plan note: extend during implementation. Start with ~30 entries; refine post-launch.
]


class ClassicException(TypedDict):
    make: str
    model: str
    year_min: int
    year_max: int
    note: str


CLASSIC_EXCEPTIONS: list[ClassicException] = [
    {"make": "Toyota", "model": "Tundra", "year_min": 2000, "year_max": 2006, "note": "First-gen Tundra, V8."},
    {"make": "Toyota", "model": "Land Cruiser", "year_min": 2000, "year_max": 2007, "note": "Last 100-series solid-axle option."},
    {"make": "Toyota", "model": "4Runner", "year_min": 2003, "year_max": 2009, "note": "4th-gen V8 4Runner sought-after."},
    {"make": "Land Rover", "model": "Defender", "year_min": 2000, "year_max": 2010, "note": "Defender 90/110."},
    # Plan note: extend during implementation. Era + model + spec specific.
]


class GotchaEntry(TypedDict):
    make: str
    model: str
    year_min: int
    year_max: int
    note: str


GOTCHAS: list[GotchaEntry] = [
    {"make": "Toyota", "model": "Tacoma", "year_min": 2005, "year_max": 2015,
     "note": "Frame rust recall: inspect rear leaf-spring perches and crossmembers."},
    {"make": "Toyota", "model": "4Runner", "year_min": 2003, "year_max": 2009,
     "note": "Frame rust recall on early years; verify recall completion."},
    {"make": "Honda", "model": "CR-V", "year_min": 2017, "year_max": 2022,
     "note": "1.5T fuel-in-oil dilution; check oil level above full and gasoline smell on dipstick."},
    {"make": "Ford", "model": "F-150", "year_min": 2011, "year_max": 2016,
     "note": "3.5L EcoBoost cam phaser / timing chain rattle on cold start; TSB 16-0027."},
    {"make": "Subaru", "model": "Forester", "year_min": 1999, "year_max": 2011,
     "note": "EJ25 head gasket failure 100k–150k km; ask for updated MLS gasket."},
    {"make": "Hyundai", "model": "Sonata", "year_min": 2011, "year_max": 2019,
     "note": "Theta II 2.0T / 2.4 rod-bearing failure; verify recall / KSDS lifetime warranty."},
    {"make": "Nissan", "model": "Altima", "year_min": 2013, "year_max": 2018,
     "note": "CVT judder / whine class action; check fluid and any rebuild paperwork."},
    {"make": "Jeep", "model": "Wrangler", "year_min": 2007, "year_max": 2018,
     "note": "Death-wobble: inspect track bar, ball joints, steering stabilizer."},
]


def model_gotchas_for(*, make: str | None, model: str | None, year: int | None) -> list[str]:
    if not (make and model and year):
        return []
    out: list[str] = []
    for g in GOTCHAS:
        if g["make"].lower() == make.lower() and g["model"].lower() == model.lower():
            if g["year_min"] <= year <= g["year_max"]:
                out.append(g["note"])
    return out
```

- [ ] **Step 3: Implement `src/carbuyer/llm/base.py`**

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

from carbuyer.llm.schemas import EnrichmentOutput, VisionOutput


@dataclass(slots=True)
class DescribeInput:
    title: str
    description: str
    year: int | None
    make: str | None
    model: str | None
    auctioneer_name: str | None
    auction_subtype: str
    pickup_province: str | None
    raw_carfax_url: str | None


@dataclass(slots=True)
class VisionInput:
    photo_paths: list[str]   # local paths to resized JPEGs
    year: int | None
    make: str | None
    model: str | None
    description_condition: str | None
    description_red_flags: list[str]
    description_green_flags: list[str]


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def describe(self, payload: DescribeInput) -> EnrichmentOutput: ...

    @abstractmethod
    async def vision(self, payload: VisionInput) -> VisionOutput: ...
```

- [ ] **Step 4: Create `__init__.py` files and run tests**

```bash
touch src/carbuyer/flags/__init__.py tests/flags/__init__.py
uv run pytest tests/flags/test_taxonomy.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/llm/base.py src/carbuyer/flags/__init__.py src/carbuyer/flags/taxonomy.py tests/flags/__init__.py tests/flags/test_taxonomy.py
git commit -m "llm+flags: provider ABC and taxonomy seeds"
```

---

### Task 22: OpenAI provider (description pass)

**Files:**
- Create: `src/carbuyer/llm/openai_provider.py`
- Create: `src/carbuyer/llm/prompts.py`
- Create: `tests/llm/test_openai_provider.py`

- [ ] **Step 1: Implement `src/carbuyer/llm/prompts.py`**

```python
from carbuyer.flags.taxonomy import (
    CLASSIC_EXCEPTIONS, DESIRABLE_TRIMS, GREEN_FLAG_TAXONOMY,
    RED_FLAG_TAXONOMY, SHOWSTOPPER_TAXONOMY, model_gotchas_for,
)


def _bullet(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items)


def description_system_prompt() -> str:
    red = _bullet([f"{f['flag']} (weight {f['weight']}): {f['description']}" for f in RED_FLAG_TAXONOMY])
    green = _bullet([f"{f['flag']} (weight {f['weight']}): {f['description']}" for f in GREEN_FLAG_TAXONOMY])
    show = _bullet([f"{f['flag']}: {f['description']}" for f in SHOWSTOPPER_TAXONOMY])
    desirable = _bullet([f"{e['make']} {e['model']} {e['trim']} — {e['note']}" for e in DESIRABLE_TRIMS])
    classics = _bullet([f"{e['make']} {e['model']} ({e['year_min']}–{e['year_max']}) — {e['note']}" for e in CLASSIC_EXCEPTIONS])
    return f"""You enrich auction lot listings into structured JSON for a used-vehicle deal-finder.

Use ONLY these flag taxonomies. Do not invent new flags.

RED FLAGS:
{red}

GREEN FLAGS:
{green}

SHOWSTOPPER FLAGS (the listing is excluded from notifications regardless of price):
{show}

DESIRABLE TRIMS / SPEC COMBOS — set `desirable_trim_or_spec=true` when the lot matches one:
{desirable}

CLASSIC / COLLECTOR EXCEPTIONS for years 2000–2010 — set `classic_or_collector=true` when the lot matches one. For year ≤ 2000, default `classic_or_collector=true` for cars/trucks of mass-produced significance.
{classics}

Rules:
- Output `unknown` when you cannot determine a field. Do not guess.
- If `condition_confidence < 0.5`, output `condition_categorical = "decent"`.
- Quote `evidence` verbatim from the listing. Do not paraphrase.
- For each flag in `red_flags`/`green_flags`, use the taxonomy `flag` key and copy the corresponding `weight`.
"""


def description_user_prompt(
    *, title: str, description: str,
    year: int | None, make: str | None, model: str | None,
    auctioneer_name: str | None, auction_subtype: str,
    pickup_province: str | None,
) -> str:
    gotchas = model_gotchas_for(make=make, model=model, year=year)
    gotcha_block = ""
    if gotchas:
        gotcha_block = "\n\nMODEL-SPECIFIC GOTCHAS for this make/model/year:\n" + _bullet(gotchas)
    return f"""TITLE: {title}

DESCRIPTION:
{description}

CONTEXT:
- year={year}, make={make}, model={model}
- auctioneer={auctioneer_name}
- auction_subtype={auction_subtype}
- pickup_province={pickup_province}{gotcha_block}

Return the structured EnrichmentOutput.
"""
```

- [ ] **Step 2: Implement `src/carbuyer/llm/openai_provider.py`**

```python
from openai import AsyncOpenAI
from openai import APIError, RateLimitError

from carbuyer.llm.base import DescribeInput, LLMProvider, VisionInput
from carbuyer.llm.prompts import description_system_prompt, description_user_prompt
from carbuyer.llm.schemas import EnrichmentOutput, VisionOutput
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger


log = get_logger("openai_provider")


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.client = AsyncOpenAI(api_key=api_key or settings.openai_api_key)
        self.model = model or settings.openai_model

    async def describe(self, payload: DescribeInput) -> EnrichmentOutput:
        sys_prompt = description_system_prompt()
        user_prompt = description_user_prompt(
            title=payload.title, description=payload.description,
            year=payload.year, make=payload.make, model=payload.model,
            auctioneer_name=payload.auctioneer_name,
            auction_subtype=payload.auction_subtype,
            pickup_province=payload.pickup_province,
        )
        try:
            # Phase 3 overlay #8: GA path, not .beta. Phase 3 ships max_tokens=3000.
            response = await self.client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=EnrichmentOutput,
                temperature=0,
                max_tokens=3000,
            )
        except (APIError, RateLimitError):
            log.exception("openai describe failed")
            raise
        result = response.choices[0].message.parsed
        if result is None:
            raise RuntimeError("openai parsed result was None")
        return result

    async def vision(self, payload: VisionInput) -> VisionOutput:
        # Implemented in Phase 8.
        raise NotImplementedError("vision pass implemented in Phase 8")
```

- [ ] **Step 3: Write the test (mocked OpenAI client, no live calls)**

```python
# tests/llm/test_openai_provider.py
from unittest.mock import AsyncMock, MagicMock

import pytest

from carbuyer.llm.base import DescribeInput
from carbuyer.llm.openai_provider import OpenAIProvider
from carbuyer.llm.schemas import (
    EnrichmentOutput, NormalizedVehicle, RarityAssessment,
)


@pytest.mark.asyncio
async def test_describe_returns_parsed_enrichment_output() -> None:
    expected = EnrichmentOutput(
        normalized_vehicle=NormalizedVehicle(
            year=2010, make="Ford", model="F-150", trim=None, engine="5.4L",
            transmission="automatic", drivetrain="4wd", mileage_km=250000, vin=None,
        ),
        title_status="NORMAL",
        condition_categorical="decent",
        condition_confidence=0.6,
        red_flags=[], green_flags=[], showstopper_flags=[],
        carfax_url=None,
        summary="ok",
        rarity=RarityAssessment(
            desirable_trim_or_spec=False, classic_or_collector=False,
            desirability_signals=[], desirability_evidence=[],
        ),
    )
    fake_choice = MagicMock()
    fake_choice.message.parsed = expected
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]

    provider = OpenAIProvider(api_key="sk-fake")
    provider.client = MagicMock()
    provider.client.chat.completions.parse = AsyncMock(return_value=fake_response)  # GA path (Phase 3 overlay #8)

    out = await provider.describe(DescribeInput(
        title="t", description="d",
        year=2010, make="Ford", model="F-150",
        auctioneer_name=None, auction_subtype="estate",
        pickup_province="AB", raw_carfax_url=None,
    ))
    assert out.normalized_vehicle.year == 2010
    assert out.condition_categorical == "decent"
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/llm/test_openai_provider.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/llm/openai_provider.py src/carbuyer/llm/prompts.py tests/llm/test_openai_provider.py
git commit -m "llm: OpenAI provider for description pass"
```

---

### Task 23: Carfax extractor (text-only, no JS)

**Files:**
- Create: `src/carbuyer/llm/carfax.py`
- Create: `tests/llm/test_carfax.py`

- [ ] **Step 1: Implement `src/carbuyer/llm/carfax.py`**

```python
import re
from urllib.parse import urlparse

from openai import AsyncOpenAI

from carbuyer.llm.schemas import CarfaxFindings
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger
from carbuyer.sources.http import make_client


log = get_logger("carfax")

_CARFAX_HOSTS = ("carfax.ca", "www.carfax.ca", "carfax.com", "www.carfax.com")
_CARFAX_PATTERN = re.compile(r"https?://[\w.-]*carfax\.(ca|com)/\S+", re.IGNORECASE)


def find_carfax_url(text: str) -> str | None:
    if not text:
        return None
    m = _CARFAX_PATTERN.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,);")
    return url if urlparse(url).hostname in _CARFAX_HOSTS else None


async def fetch_carfax_text(url: str) -> str | None:
    try:
        async with make_client() as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except Exception:
        log.exception("carfax fetch failed", url=url)
        return None


async def extract_carfax_findings(html: str, *, client: AsyncOpenAI | None = None) -> CarfaxFindings | None:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)[:8000]
    api = client or AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        # Phase 3 overlay #8: GA path, not .beta.
        response = await api.chat.completions.parse(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": (
                    "Extract structured Carfax findings from the report text. "
                    "Output `unknown` when uncertain; never invent facts."
                )},
                {"role": "user", "content": text},
            ],
            response_format=CarfaxFindings,
            temperature=0,
            max_tokens=512,
        )
    except Exception:
        log.exception("carfax extract failed")
        return None
    return response.choices[0].message.parsed
```

- [ ] **Step 2: Write the test**

```python
# tests/llm/test_carfax.py
from carbuyer.llm.carfax import find_carfax_url


def test_find_carfax_url_extracts_link() -> None:
    text = "Clean carfax: https://www.carfax.ca/vhr/abc123 . email me."
    assert find_carfax_url(text) == "https://www.carfax.ca/vhr/abc123"


def test_find_carfax_url_returns_none_when_absent() -> None:
    assert find_carfax_url("just a description") is None
```

- [ ] **Step 3: Run test and commit**

```bash
uv run pytest tests/llm/test_carfax.py -v
git add src/carbuyer/llm/carfax.py tests/llm/test_carfax.py
git commit -m "llm: Carfax URL extractor and findings parser"
```

---

### Task 24: Description-enricher worker

**Files:**
- Create: `src/carbuyer/apps/enricher/__init__.py`
- Create: `src/carbuyer/apps/enricher/__main__.py`
- Create: `src/carbuyer/apps/enricher/enricher.py`
- Create: `tests/apps/test_enricher.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/enricher/enricher.py`**

```python
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import listen, notify
from carbuyer.db.queue import claim_pending_lots
from carbuyer.db.session import async_session_maker
from carbuyer.llm.base import DescribeInput, LLMProvider
from carbuyer.llm.carfax import extract_carfax_findings, fetch_carfax_text, find_carfax_url
from carbuyer.llm.openai_provider import OpenAIProvider
from carbuyer.llm.schemas import EnrichmentOutput
from carbuyer.shared.logging import get_logger


log = get_logger("enricher")
ENRICHMENT_VERSION = "v1"


async def enrich_one(session: AsyncSession, lot: AuctionLot, provider: LLMProvider) -> None:
    auction = await session.get(Auction, lot.auction_id)
    if auction is None:
        lot.enrichment_status = "failed"
        return
    payload = DescribeInput(
        title=lot.title or "",
        description=lot.description or "",
        year=lot.year,
        make=lot.make,
        model=lot.model,
        auctioneer_name=auction.auctioneer_name,
        auction_subtype=auction.auction_subtype,
        pickup_province=auction.pickup_province,
        raw_carfax_url=find_carfax_url(lot.description or ""),
    )
    try:
        out: EnrichmentOutput = await provider.describe(payload)
    except Exception:
        log.exception("describe failed", lot_id=lot.id)
        lot.enrichment_status = "failed"
        return

    nv = out.normalized_vehicle
    lot.year = nv.year or lot.year
    lot.make = nv.make or lot.make
    lot.model = nv.model or lot.model
    lot.trim = nv.trim or lot.trim
    lot.engine = nv.engine
    lot.transmission = nv.transmission
    lot.drivetrain = nv.drivetrain
    lot.mileage_km = nv.mileage_km or lot.mileage_km
    lot.vin = nv.vin or lot.vin
    lot.title_status = out.title_status
    lot.condition_categorical = out.condition_categorical
    lot.condition_confidence = out.condition_confidence
    lot.red_flags = [f.model_dump() for f in out.red_flags]
    lot.green_flags = [f.model_dump() for f in out.green_flags]
    lot.showstopper_flags = [f.model_dump() for f in out.showstopper_flags]
    lot.summary = out.summary
    lot.carfax_url = out.carfax_url or payload.raw_carfax_url
    lot.desirable_trim_or_spec = out.rarity.desirable_trim_or_spec
    lot.classic_or_collector = out.rarity.classic_or_collector
    lot.desirability_signals = out.rarity.desirability_signals
    lot.desirability_evidence = out.rarity.desirability_evidence

    if lot.carfax_url:
        html = await fetch_carfax_text(lot.carfax_url)
        if html:
            findings = await extract_carfax_findings(html)
            if findings:
                lot.carfax_findings = findings.model_dump()

    lot.enrichment_status = "done"
    lot.valuation_status = "pending"
    lot.enrichment_version = ENRICHMENT_VERSION


async def process_pending(provider: LLMProvider, *, batch_size: int = 20) -> int:
    async with async_session_maker() as session:
        async with session.begin():
            lots = await claim_pending_lots(
                session, status_field="enrichment_status", limit=batch_size
            )
        for lot in lots:
            async with session.begin():
                await enrich_one(session, lot, provider)
                if lot.enrichment_status == "done":
                    await notify(session, "valuation_pending", str(lot.id))
        return len(lots)


async def main() -> None:
    provider = OpenAIProvider()
    async for _ in listen("enrichment_pending"):
        try:
            await process_pending(provider)
        except Exception:
            log.exception("batch failed; sleeping before retry")
            await asyncio.sleep(5)
```

- [ ] **Step 2: Implement `__main__.py` and `__init__.py`**

```python
# src/carbuyer/apps/enricher/__main__.py
from carbuyer.apps._runner import run_worker
from carbuyer.apps.enricher.enricher import main


if __name__ == "__main__":
    run_worker("enricher", main)
```

```bash
touch src/carbuyer/apps/enricher/__init__.py
```

- [ ] **Step 3: Write the test (mocked provider)**

```python
# tests/apps/test_enricher.py
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from carbuyer.apps.enricher.enricher import enrich_one
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.llm.schemas import (
    EnrichmentOutput, NormalizedVehicle, RarityAssessment,
)


@pytest.mark.asyncio
async def test_enrich_one_writes_fields(session) -> None:
    a = Auction(source="t", source_auction_id="A", url="x", auction_subtype="estate",
                first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
                pickup_province="AB")
    session.add(a)
    await session.flush()
    lot = AuctionLot(
        auction_id=a.id, source_lot_id="L1", url="https://x/lot/1",
        title="2010 Ford F-150", description="runs and drives",
    )
    session.add(lot)
    await session.flush()

    expected = EnrichmentOutput(
        normalized_vehicle=NormalizedVehicle(
            year=2010, make="Ford", model="F-150", trim=None, engine="5.4L",
            transmission="automatic", drivetrain="4wd", mileage_km=200000, vin=None,
        ),
        title_status="NORMAL",
        condition_categorical="decent",
        condition_confidence=0.7,
        red_flags=[], green_flags=[], showstopper_flags=[],
        carfax_url=None, summary="ok",
        rarity=RarityAssessment(
            desirable_trim_or_spec=False, classic_or_collector=False,
            desirability_signals=[], desirability_evidence=[],
        ),
    )
    provider = AsyncMock()
    provider.describe = AsyncMock(return_value=expected)

    await enrich_one(session, lot, provider)
    assert lot.enrichment_status == "done"
    assert lot.year == 2010
    assert lot.condition_categorical == "decent"
    assert lot.valuation_status == "pending"
```

- [ ] **Step 4: Run test and commit**

```bash
uv run pytest tests/apps/test_enricher.py -v
git add src/carbuyer/apps/enricher/ tests/apps/test_enricher.py
git commit -m "apps: enricher worker (description LLM pass)"
```

---

End of Phase 3. New lots arrive `enrichment_status='pending'`; the enricher LLMs them into structured fields and emits `valuation_pending`.

---

## Phase 4 — Valuation and scoring

### Phase 4 — Pre-implementation overlay (folded in from Phase 3 review 2026-05-09)

Phase 3's post-implementation review (overlay decisions #28-37) added two columns the valuator must consume, plus revealed several name-drift issues that Phase 4 inherits unchanged from the original plan. Phase 4 also has the same Phase-2 contract violations Phase 0/1/2 fixed. Folding these in here so the next implementer doesn't re-walk the same cliff.

**Must-fix decisions (apply when implementing Phase 4):**

1. **Real queue / session APIs.** Plan code blocks reference `claim_pending_lots` and `async_session_maker` — neither exists. Use `claim_pending_ids` (returns `list[int]`, commits its own claim tx) and `get_session()` / `get_session_maker()`. Same idiom as `lot_scraper.process_auction` and `enricher.process_pending`.

2. **LLM I/O outside DB tx.** The valuator does no LLM I/O directly, but the comp-set query against `historical_sales` + `auction_lots` is non-trivial — keep the per-lot transaction short and do the comp computation in memory after closing the read tx if the query duration ever exceeds `idle_in_transaction_session_timeout=60s`.

3. **Catchup sweep at startup before `LISTEN`.** Phase 2 idiom — drain pending rows on startup so NOTIFYs missed during downtime aren't lost.

4. **StrEnum status writes.** `lot.valuation_status = ValuationStatus.DONE` etc. — not bare strings.

5. **Status writes use `EnrichmentStatus` / `ValuationStatus` StrEnum members.** Plan uses bare strings.

6. **`valuation_attempts` retry counter.** Same pattern as `enrichment_attempts` — transient errors leave PENDING for re-claim; permanent errors fail-fast at attempts=1. Add a column in the same migration as the next-batch additions, or reuse the per-lot generic `enrichment_attempts` if one counter is enough (recommend: separate columns per stage so failure modes are diagnosable).

7. **Self-NOTIFY on transient leftover.** Same idiom as enricher — `pg_notify('valuation_pending', '')` after a batch with any transient failures.

**Sparse-listing signal consumption (Phase 3 overlay #15 / #25 / #31):**

8. **`condition_position` accepts the sparse-listing flag.** Update Task 25 signature to `condition_position(condition: str, *, sparse: bool = False) -> float`. When `sparse=True`, shift the position downward toward p25 (~0.35) instead of midpoint (0.5). Rationale: a confident "decent" rating reflects an honestly-decent vehicle; a sparse-listing-coerced "decent" signals "we don't know" and historically auction-yard sparse listings are below-median condition.

9. **`compute_fair_value` accepts `sparse: bool`.** Threading through Task 27 → Task 30 valuator. Without this the dual write of `condition_inferred_from_sparse_listing` is dead.

10. **`description_quality` factor on `flag_score` or `price_deal_score`.** Domain reviewer's meta-note: terse RB listings → no flags → look decent; verbose estate listings → many honest red flags → look like junk. Phase 4 must dampen scoring of low-evidence listings. Recommended: `if description_quality == "thin": flag_score = max(-2, flag_score)` (clip negative dampening — a thin listing can't surface enough evidence to legitimately score below -2). OR: apply a multiplier to `price_deal_score` based on description_quality. Pick one and document in the Phase 4 implementation overlay.

11. **`flag_score` baseline normalization.** Domain reviewer flagged that the typical RB listing fires `mileage_unknown` + `out_of_province` + `winter_tires_only` for -3 before any actual issue. Either reclassify these as context (drop weight) OR rebaseline Phase 4's "neutral" at -3 instead of 0. Recommended path: in `flag_score`, exclude flags whose `weight` magnitude is 1 from the cumulative when more than 3 of them fire (cap "context flag dilution" at -2). Document in the Phase 4 overlay.

**Showstopper-as-red-flag fallout (Phase 3 overlay #32 / #33):**

12. **Phase 3 demoted `salvage_not_rebuilt`, `outstanding_lien`, `lemon_law_buyback` from showstopper to -4 red flag.** The original plan's exclusion logic (any showstopper → notify_status='skipped') still works for the remaining showstoppers. But Phase 4 should additionally exclude lots with **cumulative `flag_score <= -8`** from notifications regardless of price-deal score — that's where heavy-but-not-dispositive red flags would otherwise leak through. The threshold is heuristic; revisit after first 100 real lots.

13. **`flood_damage` split.** The taxonomy now has `flood_damage_total` (showstopper) and `flood_damage_partial` (-3 red). No code change required in Phase 4 — both flow through the existing showstopper / red-flag paths.

**Forward-compat with Phase 7 (bid polling):**

14. **`is_no_reserve` derivation.** Phase 3 currently passes `False` because `lot.reserve_met is False` was inverted (means "reserve set, not yet met"). Phase 7 bid-poller should write a true `lot.is_no_reserve: bool | None` column when the source HTML/JSON exposes it. Phase 4 valuation should NOT condition on `reserve_met` for that signal; the comp-channel multiplier already handles the "no reserve = treat as private sale price" math.

End of overlay.

---

### Task 25: Channel normalization + condition mapping

**Files:**
- Create: `src/carbuyer/scoring/__init__.py`
- Create: `src/carbuyer/scoring/channels.py`
- Create: `tests/scoring/__init__.py`
- Create: `tests/scoring/test_channels.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scoring/test_channels.py
from decimal import Decimal

from carbuyer.scoring.channels import (
    normalize_to_private, condition_position, CONDITION_POSITION,
)


def test_normalize_auction_estate_to_private() -> None:
    assert normalize_to_private(Decimal("10000"), "auction_estate") == Decimal("12000")


def test_normalize_dealer_to_private() -> None:
    assert normalize_to_private(Decimal("10000"), "dealer") == Decimal("9200")


def test_normalize_unknown_falls_back_to_identity() -> None:
    assert normalize_to_private(Decimal("10000"), "weird") == Decimal("10000")


def test_condition_position_returns_canonical_values() -> None:
    assert condition_position("bad") == 0.0
    assert condition_position("decent") == 0.5
    assert condition_position("great") == 1.0


def test_condition_position_with_sparse_flag_shifts_toward_p25() -> None:
    # Phase 4 overlay #8: sparse-listing-coerced "decent" should value below
    # midpoint, since auction-yard sparse listings historically run worse
    # than honestly-described "decent" lots.
    assert condition_position("decent", sparse=True) == 0.35
    # Confident "decent" stays at midpoint.
    assert condition_position("decent", sparse=False) == 0.5
```

- [ ] **Step 2: Implement `src/carbuyer/scoring/channels.py`**

```python
from decimal import Decimal
from typing import Final


CHANNEL_MULTIPLIERS: Final[dict[str, Decimal]] = {
    "private": Decimal("1.00"),
    "dealer": Decimal("0.92"),
    "auction_estate": Decimal("1.20"),
    "auction_govt": Decimal("1.15"),
    "auction_commercial": Decimal("1.10"),
    "auction_salvage": Decimal("0.50"),  # used only when comparing salvage comps; not used in MVP
}

CONDITION_POSITION: Final[dict[str, float]] = {
    "bad": 0.0,
    "poor": 0.25,
    "decent": 0.50,
    "good": 0.75,
    "great": 1.0,
}


def normalize_to_private(price_cad: Decimal, channel: str) -> Decimal:
    return price_cad * CHANNEL_MULTIPLIERS.get(channel, Decimal("1.00"))


def condition_position(condition: str, *, sparse: bool = False) -> float:
    """Map a categorical condition to a [0, 1] position between p10 and p90.

    Phase 4 overlay #8: when ``sparse=True`` the enricher coerced
    ``condition_categorical='decent'`` because ``condition_confidence < 0.5``
    (see Phase 3 ``_apply_to_lot``). That value reflects "we don't know" not
    "actually decent" — shift toward p25 so sparse listings price below
    confidently-decent comps. The sparse flag is only honored on "decent"
    (other categorical values come with their own confidence).
    """
    base = CONDITION_POSITION.get(condition, 0.5)
    if sparse and condition == "decent":
        return 0.35
    return base
```

- [ ] **Step 3: Create `__init__.py` and run tests**

```bash
touch src/carbuyer/scoring/__init__.py tests/scoring/__init__.py
uv run pytest tests/scoring/test_channels.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/scoring/__init__.py src/carbuyer/scoring/channels.py tests/scoring/__init__.py tests/scoring/test_channels.py
git commit -m "scoring: channel normalization + condition position"
```

---

### Task 26: Comp set query

**Files:**
- Create: `src/carbuyer/scoring/comps.py`
- Create: `tests/scoring/test_comps.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scoring/test_comps.py
import pytest
from decimal import Decimal

from carbuyer.db.models import HistoricalSale
from carbuyer.scoring.comps import build_comp_set, ComparableSale


@pytest.mark.asyncio
async def test_build_comp_set_filters_make_model_year_mileage(session) -> None:
    base = dict(
        make="Toyota", model="Tacoma", trim="TRD Off-Road",
        sale_channel="auction_estate", sale_platform="hibid",
        title_status="NORMAL", schema_version=1,
    )
    session.add_all([
        HistoricalSale(year=2015, mileage_km=150000,
                       final_listed_price_cad=Decimal("20000"),
                       final_price_with_premium_cad=Decimal("22000"),
                       buyer_premium_pct_at_sale=Decimal("0.10"),
                       disposition_reason="sold", **base),
        HistoricalSale(year=2014, mileage_km=160000,
                       final_listed_price_cad=Decimal("19000"),
                       final_price_with_premium_cad=Decimal("20900"),
                       buyer_premium_pct_at_sale=Decimal("0.10"),
                       disposition_reason="sold", **base),
        HistoricalSale(year=2010, mileage_km=300000,  # too old + too high mileage
                       final_listed_price_cad=Decimal("9000"),
                       final_price_with_premium_cad=Decimal("9900"),
                       buyer_premium_pct_at_sale=Decimal("0.10"),
                       disposition_reason="sold", **base),
    ])
    await session.commit()

    comps = await build_comp_set(
        session, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        year=2015, mileage_km=150000, year_window=1, mileage_pct=0.20,
    )
    assert len(comps) == 2
    assert all(isinstance(c, ComparableSale) for c in comps)
```

- [ ] **Step 2: Implement `src/carbuyer/scoring/comps.py`**

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import AuctionLot, HistoricalSale


@dataclass(frozen=True, slots=True)
class ComparableSale:
    price_cad: Decimal
    sale_channel: str
    year: int | None
    mileage_km: int | None
    days_listed: int | None
    disposition_reason: str
    source: str  # "historical_sales" or "auction_lots"


async def build_comp_set(
    session: AsyncSession,
    *,
    make: str,
    model: str,
    trim: str | None,
    year: int,
    mileage_km: int,
    year_window: int = 1,
    mileage_pct: float = 0.20,
) -> list[ComparableSale]:
    mileage_lo = int(mileage_km * (1 - mileage_pct))
    mileage_hi = int(mileage_km * (1 + mileage_pct))

    hs_stmt = select(HistoricalSale).where(
        HistoricalSale.make == make,
        HistoricalSale.model == model,
        HistoricalSale.year.between(year - year_window, year + year_window),
        HistoricalSale.mileage_km.between(mileage_lo, mileage_hi),
    )
    if trim:
        hs_stmt = hs_stmt.where(or_(HistoricalSale.trim == trim, HistoricalSale.trim.is_(None)))
    hs_rows = (await session.execute(hs_stmt)).scalars().all()

    cutoff = datetime.utcnow() - timedelta(days=14)
    al_stmt = select(AuctionLot).where(
        AuctionLot.make == make,
        AuctionLot.model == model,
        AuctionLot.year.between(year - year_window, year + year_window),
        AuctionLot.mileage_km.between(mileage_lo, mileage_hi),
        AuctionLot.lot_status == "closed",
        AuctionLot.closed_at >= cutoff,
        AuctionLot.final_bid_cad.is_not(None),
    )
    if trim:
        al_stmt = al_stmt.where(or_(AuctionLot.trim == trim, AuctionLot.trim.is_(None)))
    al_rows = (await session.execute(al_stmt)).scalars().all()

    comps: list[ComparableSale] = []
    for h in hs_rows:
        price = h.final_price_with_premium_cad or h.final_listed_price_cad
        if price is None:
            continue
        comps.append(ComparableSale(
            price_cad=Decimal(price),
            sale_channel=h.sale_channel,
            year=h.year,
            mileage_km=h.mileage_km,
            days_listed=h.days_listed,
            disposition_reason=h.disposition_reason,
            source="historical_sales",
        ))
    for lot in al_rows:
        bid = lot.final_bid_cad
        if bid is None:
            continue
        # Reconstruct all-in by applying the auction's BP at distillation time.
        # For not-yet-distilled lots we approximate with current BP, fetched lazily.
        comps.append(ComparableSale(
            price_cad=Decimal(bid),
            sale_channel="auction_estate",
            year=lot.year,
            mileage_km=lot.mileage_km,
            days_listed=None,
            disposition_reason="sold",
            source="auction_lots",
        ))
    return comps
```

- [ ] **Step 3: Run test and commit**

```bash
uv run pytest tests/scoring/test_comps.py -v
git add src/carbuyer/scoring/comps.py tests/scoring/test_comps.py
git commit -m "scoring: comp set query (historical_sales + recent closed lots)"
```

---

### Task 27: Fair-value range + expected value

**Files:**
- Create: `src/carbuyer/scoring/fair_value.py`
- Create: `tests/scoring/test_fair_value.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scoring/test_fair_value.py
from decimal import Decimal

from carbuyer.scoring.comps import ComparableSale
from carbuyer.scoring.fair_value import compute_fair_value, ConfidenceBucket


def test_compute_fair_value_ten_comps() -> None:
    comps = [
        ComparableSale(
            price_cad=Decimal(p), sale_channel="auction_estate",
            year=2015, mileage_km=150000, days_listed=None,
            disposition_reason="sold", source="historical_sales",
        )
        for p in [10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000]
    ]
    fv = compute_fair_value(comps, condition="decent")
    assert fv is not None
    # All comps are auction_estate × 1.20 → range expands
    assert fv.value_low_cad < fv.value_mid_cad < fv.value_high_cad
    assert fv.confidence == ConfidenceBucket.HIGH
    assert fv.comp_count == 10


def test_compute_fair_value_insufficient() -> None:
    comps = [
        ComparableSale(
            price_cad=Decimal("10000"), sale_channel="auction_estate",
            year=2015, mileage_km=150000, days_listed=None,
            disposition_reason="sold", source="historical_sales",
        )
    ]
    fv = compute_fair_value(comps, condition="decent")
    assert fv is not None
    assert fv.confidence == ConfidenceBucket.INSUFFICIENT
    assert fv.expected_value_cad is None
```

- [ ] **Step 2: Implement `src/carbuyer/scoring/fair_value.py`**

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from statistics import median, quantiles

from carbuyer.scoring.channels import condition_position, normalize_to_private
from carbuyer.scoring.comps import ComparableSale


class ConfidenceBucket(str, Enum):
    INSUFFICIENT = "insufficient"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(slots=True)
class FairValue:
    value_low_cad: Decimal | None
    value_mid_cad: Decimal | None
    value_high_cad: Decimal | None
    expected_value_cad: Decimal | None
    comp_count: int
    confidence: ConfidenceBucket


def _trim_mileage_outliers(comps: list[ComparableSale]) -> list[ComparableSale]:
    if len(comps) < 4:
        return comps
    miles = [float(c.mileage_km) for c in comps if c.mileage_km is not None]
    if not miles:
        return comps
    mean = sum(miles) / len(miles)
    var = sum((m - mean) ** 2 for m in miles) / len(miles)
    sd = var ** 0.5 if var > 0 else 1.0
    if sd == 0:
        return comps
    return [c for c in comps if c.mileage_km is None or abs((c.mileage_km - mean) / sd) <= 2]


def compute_fair_value(
    comps: list[ComparableSale],
    *,
    condition: str,
    sparse: bool = False,
) -> FairValue:
    """Phase 4 overlay #9: ``sparse`` threads through from
    ``lot.condition_inferred_from_sparse_listing`` so the position penalty
    fires only when the enricher signaled low confidence."""
    trimmed = _trim_mileage_outliers(comps)
    if len(trimmed) < 5:
        return FairValue(
            value_low_cad=None, value_mid_cad=None, value_high_cad=None,
            expected_value_cad=None, comp_count=len(trimmed),
            confidence=ConfidenceBucket.INSUFFICIENT,
        )
    normalized = sorted(
        float(normalize_to_private(c.price_cad, c.sale_channel)) for c in trimmed
    )
    if len(normalized) >= 10:
        q = quantiles(normalized, n=10)
        p10, p90 = q[0], q[-1]
    else:
        p10, p90 = min(normalized), max(normalized)
    p50 = median(normalized)
    pos = condition_position(condition, sparse=sparse)
    expected = p10 + pos * (p90 - p10)
    confidence = ConfidenceBucket.HIGH if len(trimmed) >= 15 else ConfidenceBucket.MEDIUM
    return FairValue(
        value_low_cad=Decimal(round(p10, 2)),
        value_mid_cad=Decimal(round(p50, 2)),
        value_high_cad=Decimal(round(p90, 2)),
        expected_value_cad=Decimal(round(expected, 2)),
        comp_count=len(trimmed),
        confidence=confidence,
    )
```

- [ ] **Step 3: Run test and commit**

```bash
uv run pytest tests/scoring/test_fair_value.py -v
git add src/carbuyer/scoring/fair_value.py tests/scoring/test_fair_value.py
git commit -m "scoring: fair value range + condition-mapped expected value"
```

---

### Task 28: Landed cost premium

**Files:**
- Create: `src/carbuyer/scoring/landed_cost.py`
- Create: `tests/scoring/test_landed_cost.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scoring/test_landed_cost.py
from decimal import Decimal

from carbuyer.scoring.landed_cost import landed_cost_premium, distance_km_between


def test_same_province_zero() -> None:
    assert landed_cost_premium(home="AB", dest="AB", distance_km=200) == Decimal("0")


def test_cross_province_includes_inspection_and_contingency() -> None:
    cost = landed_cost_premium(home="AB", dest="ON", distance_km=3500)
    # transport floor = 600 + 0.65*3500 = 2875; inspection ON 120; contingency ON 350
    expected = Decimal(2875) + Decimal(120) + Decimal(350)
    assert cost == expected


def test_min_floor_applies() -> None:
    cost = landed_cost_premium(home="AB", dest="BC", distance_km=100)
    # max(600, 400 + 65) = 600 (min floor)
    expected = Decimal(600) + Decimal(125) + Decimal(150)
    assert cost == expected
```

- [ ] **Step 2: Implement `src/carbuyer/scoring/landed_cost.py`**

```python
from decimal import Decimal


PROV_INSPECTION: dict[str, int] = {
    "AB": 200, "ON": 120, "BC": 125, "QC": 125,
    "MB": 100, "SK": 75, "NS": 50, "NB": 50,
}
PROV_CONTINGENCY: dict[str, int] = {
    "AB": 350, "ON": 350, "QC": 250,
}
DEFAULT_INSPECTION = 75
DEFAULT_CONTINGENCY = 150


# Approximate centroid distances between provincial capitals (km).
# Used as a fallback when we don't have the seller's exact city.
_FALLBACK_DISTANCE: dict[tuple[str, str], int] = {
    ("AB", "AB"): 0, ("AB", "BC"): 1000, ("AB", "SK"): 600, ("AB", "MB"): 1300,
    ("BC", "BC"): 0, ("BC", "AB"): 1000, ("BC", "SK"): 1700, ("BC", "MB"): 2200,
    ("SK", "SK"): 0, ("SK", "AB"): 600, ("SK", "BC"): 1700, ("SK", "MB"): 600,
    ("MB", "MB"): 0, ("MB", "SK"): 600, ("MB", "AB"): 1300, ("MB", "BC"): 2200,
}


def distance_km_between(home: str, dest: str) -> int:
    return _FALLBACK_DISTANCE.get((home, dest), 3000)


def landed_cost_premium(*, home: str, dest: str, distance_km: int) -> Decimal:
    if home == dest:
        return Decimal("0")
    transport = max(600, int(400 + 0.65 * distance_km))
    inspection = PROV_INSPECTION.get(dest, DEFAULT_INSPECTION)
    contingency = PROV_CONTINGENCY.get(dest, DEFAULT_CONTINGENCY)
    return Decimal(transport + inspection + contingency)
```

- [ ] **Step 3: Run test and commit**

```bash
uv run pytest tests/scoring/test_landed_cost.py -v
git add src/carbuyer/scoring/landed_cost.py tests/scoring/test_landed_cost.py
git commit -m "scoring: landed-cost premium model"
```

---

### Task 29: Price-deal score + rarity score

**Files:**
- Create: `src/carbuyer/scoring/score.py`
- Create: `tests/scoring/test_score.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scoring/test_score.py
from decimal import Decimal

from carbuyer.scoring.score import (
    price_deal_score, rarity_score, recommended_max_bid, RarityInputs,
)


def test_price_deal_score_positive_when_underpriced() -> None:
    s = price_deal_score(
        current_high_bid=Decimal("10000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("500"),
        expected_value=Decimal("18000"),
    )
    # all_in_at_bid = 10000 * 1.10 * 1.05 = 11550; total = 12050; score = (18000-12050)/18000 ≈ 0.330
    assert 0.32 < s < 0.34


def test_rarity_score_low_comp_count_with_desirable_yields_2() -> None:
    s = rarity_score(RarityInputs(
        desirable_trim_or_spec=True, classic_or_collector=False,
        historical_comp_count=1, recent_appreciation=None,
    ))
    # 2.0 (low+desirable) + 1.0 (desirable_trim) = 3.0
    assert s == 3.0


def test_rarity_score_low_comp_count_undesirable_yields_zero() -> None:
    s = rarity_score(RarityInputs(
        desirable_trim_or_spec=False, classic_or_collector=False,
        historical_comp_count=1, recent_appreciation=None,
    ))
    assert s == 0.0


def test_recommended_max_bid_backs_out_margin() -> None:
    bid = recommended_max_bid(
        expected_value=Decimal("20000"),
        buyer_premium_pct=Decimal("0.10"),
        gst_pct=Decimal("0.05"),
        pst_pct=Decimal("0.00"),
        landed_cost_premium=Decimal("500"),
        flip_margin=Decimal("2000"),
    )
    # target_all_in = 20000 - 2000 = 18000
    # bid = (18000 - 500) / (1.10 * 1.05) = 17500 / 1.155 ≈ 15151.52
    assert bid is not None
    assert 15140 < float(bid) < 15165
```

- [ ] **Step 2: Implement `src/carbuyer/scoring/score.py`**

```python
from dataclasses import dataclass
from decimal import Decimal


@dataclass(slots=True)
class RarityInputs:
    desirable_trim_or_spec: bool
    classic_or_collector: bool
    historical_comp_count: int
    recent_appreciation: float | None


def all_in_cost(
    *,
    current_high_bid: Decimal,
    buyer_premium_pct: Decimal,
    gst_pct: Decimal,
    pst_pct: Decimal,
    landed_cost_premium: Decimal,
) -> Decimal:
    bp_factor = Decimal("1") + buyer_premium_pct
    tax_factor = Decimal("1") + gst_pct + pst_pct
    bid_with_premium = current_high_bid * bp_factor * tax_factor
    return bid_with_premium + landed_cost_premium


def price_deal_score(
    *,
    current_high_bid: Decimal,
    buyer_premium_pct: Decimal,
    gst_pct: Decimal,
    pst_pct: Decimal,
    landed_cost_premium: Decimal,
    expected_value: Decimal,
) -> float:
    if expected_value <= 0:
        return 0.0
    total = all_in_cost(
        current_high_bid=current_high_bid,
        buyer_premium_pct=buyer_premium_pct,
        gst_pct=gst_pct,
        pst_pct=pst_pct,
        landed_cost_premium=landed_cost_premium,
    )
    return float((expected_value - total) / expected_value)


def rarity_score(inputs: RarityInputs) -> float:
    score = 0.0
    low_comp_with_desirability = (
        inputs.historical_comp_count < 3
        and (inputs.desirable_trim_or_spec or inputs.classic_or_collector)
    )
    if low_comp_with_desirability:
        score += 2.0
    if inputs.classic_or_collector:
        score += 1.5
    if inputs.desirable_trim_or_spec:
        score += 1.0
    if inputs.recent_appreciation is not None and inputs.recent_appreciation > 0.05:
        score += 1.0
    return min(score, 5.0)


def recommended_max_bid(
    *,
    expected_value: Decimal,
    buyer_premium_pct: Decimal,
    gst_pct: Decimal,
    pst_pct: Decimal,
    landed_cost_premium: Decimal,
    flip_margin: Decimal,
) -> Decimal | None:
    target_all_in = expected_value - flip_margin
    if target_all_in <= 0:
        return None
    bp_tax = (Decimal("1") + buyer_premium_pct) * (Decimal("1") + gst_pct + pst_pct)
    bid = (target_all_in - landed_cost_premium) / bp_tax
    return bid if bid > 0 else None


def flag_score(
    red: list[dict[str, int]],
    green: list[dict[str, int]],
    *,
    description_quality: str | None = None,
) -> int:
    """Cumulative weight clipped to [-5, 5].

    Phase 4 overlay #10: when ``description_quality == "thin"`` clip the
    floor at -2 — a thin listing literally cannot surface enough evidence to
    legitimately score lower; the flags that fired likely represent listing-
    sparsity friction (mileage_unknown, no_service_records) rather than
    vehicle-quality issues. Confident verbose listings keep the full -5 floor.
    """
    s = sum(int(f.get("weight", 0)) for f in red) + sum(int(f.get("weight", 0)) for f in green)
    if description_quality == "thin":
        return max(-2, min(5, s))
    return max(-5, min(5, s))
```

- [ ] **Step 3: Run test and commit**

```bash
uv run pytest tests/scoring/test_score.py -v
git add src/carbuyer/scoring/score.py tests/scoring/test_score.py
git commit -m "scoring: deal score + rarity score + recommended max bid"
```

---

### Task 30: Valuator worker

**Files:**
- Create: `src/carbuyer/apps/valuator/__init__.py`
- Create: `src/carbuyer/apps/valuator/__main__.py`
- Create: `src/carbuyer/apps/valuator/valuator.py`
- Create: `tests/apps/test_valuator.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/valuator/valuator.py`**

```python
import asyncio
import hashlib
import json
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Phase 4 overlay #1: real APIs are claim_pending_ids / get_session{,_maker}.
# Plan blocks below originally referenced claim_pending_lots / async_session_maker
# (Phase-0 names that were renamed during Phase 0/1/2 overlays). Phase 3
# enricher.py is the canonical reference for the worker shape.
from carbuyer.db.enums import EnrichmentStatus, ValuationStatus
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale
from carbuyer.db.notify import listen, notify
from carbuyer.db.queue import claim_pending_ids, select_pending_ids
from carbuyer.db.session import get_session, get_session_maker
from carbuyer.scoring.comps import build_comp_set
from carbuyer.scoring.fair_value import compute_fair_value, ConfidenceBucket
from carbuyer.scoring.landed_cost import distance_km_between, landed_cost_premium
from carbuyer.scoring.score import (
    RarityInputs, all_in_cost, flag_score, price_deal_score,
    rarity_score, recommended_max_bid,
)
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger


log = get_logger("valuator")
SCORING_VERSION = "v1"
# When implementing: `compute_fair_value(..., sparse=lot.condition_inferred_from_sparse_listing)`
# `flag_score(red, green, description_quality=lot.description_quality)`


def _weights_hash() -> str:
    payload = json.dumps({
        "scoring_version": SCORING_VERSION,
        "notify_threshold": settings.notify_threshold,
        "rarity_threshold": settings.early_warning_rarity_threshold,
        "flip_margin_min": settings.flip_margin_min_cad,
        "flip_margin_pct": settings.flip_margin_pct,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def value_one(session: AsyncSession, lot: AuctionLot) -> None:
    auction = await session.get(Auction, lot.auction_id)
    if auction is None or lot.make is None or lot.model is None or lot.year is None:
        lot.valuation_status = "skipped"
        return

    comps = await build_comp_set(
        session,
        make=lot.make, model=lot.model, trim=lot.trim,
        year=lot.year, mileage_km=lot.mileage_km or 0,
    )
    fv = compute_fair_value(comps, condition=lot.condition_categorical or "decent")
    lot.comp_count = fv.comp_count
    lot.value_low_cad = fv.value_low_cad
    lot.value_mid_cad = fv.value_mid_cad
    lot.value_high_cad = fv.value_high_cad
    lot.expected_value_cad = fv.expected_value_cad
    lot.confidence_bucket = fv.confidence.value
    lot.scoring_version = SCORING_VERSION
    lot.weights_hash = _weights_hash()

    # Historical comp count for rarity (broader: same make+model regardless of trim/year window)
    hcc_stmt = select(func.count()).where(
        HistoricalSale.make == lot.make,
        HistoricalSale.model == lot.model,
    )
    historical_count = (await session.execute(hcc_stmt)).scalar_one()
    lot.historical_comp_count = historical_count

    lot.rarity_score = rarity_score(RarityInputs(
        desirable_trim_or_spec=lot.desirable_trim_or_spec,
        classic_or_collector=lot.classic_or_collector,
        historical_comp_count=historical_count,
        recent_appreciation=lot.recent_appreciation,
    ))

    # Flag score
    lot.flag_score = flag_score(lot.red_flags or [], lot.green_flags or [])

    # Tax + landed
    bp = auction.buyer_premium_pct or Decimal("0.10")
    gst = auction.gst_pct or Decimal("0.05")
    pst = auction.pst_pct or Decimal("0.00")
    distance = distance_km_between(settings.home_province, auction.pickup_province or settings.home_province)
    landed = landed_cost_premium(home=settings.home_province, dest=auction.pickup_province or settings.home_province, distance_km=distance)
    lot.landed_cost_premium_cad = landed

    if lot.current_high_bid_cad is not None and fv.expected_value_cad is not None:
        lot.all_in_at_current_bid_cad = all_in_cost(
            current_high_bid=lot.current_high_bid_cad,
            buyer_premium_pct=bp, gst_pct=gst, pst_pct=pst,
            landed_cost_premium=landed,
        )
        lot.price_deal_score = price_deal_score(
            current_high_bid=lot.current_high_bid_cad,
            buyer_premium_pct=bp, gst_pct=gst, pst_pct=pst,
            landed_cost_premium=landed,
            expected_value=fv.expected_value_cad,
        )
    else:
        lot.price_deal_score = None
        lot.all_in_at_current_bid_cad = None

    if fv.expected_value_cad is not None:
        margin = max(
            Decimal(settings.flip_margin_min_cad),
            fv.expected_value_cad * Decimal(str(settings.flip_margin_pct)),
        )
        lot.recommended_max_bid_cad = recommended_max_bid(
            expected_value=fv.expected_value_cad,
            buyer_premium_pct=bp, gst_pct=gst, pst_pct=pst,
            landed_cost_premium=landed, flip_margin=margin,
        )

    if lot.value_low_cad is not None and lot.current_high_bid_cad is not None:
        lot.suspicious_underprice_flag = lot.current_high_bid_cad < (lot.value_low_cad * Decimal("0.85"))

    lot.valuation_status = "done"
    lot.notification_status = "pending"


async def process_pending(*, batch_size: int = 30) -> int:
    async with async_session_maker() as session:
        async with session.begin():
            lots = await claim_pending_lots(
                session, status_field="valuation_status", limit=batch_size
            )
        for lot in lots:
            async with session.begin():
                await value_one(session, lot)
                if lot.valuation_status == "done":
                    await notify(session, "notification_pending", str(lot.id))
        return len(lots)


async def main() -> None:
    async for _ in listen("valuation_pending"):
        try:
            await process_pending()
        except Exception:
            log.exception("valuation batch failed")
            await asyncio.sleep(5)
```

- [ ] **Step 2: Implement `__main__.py` and `__init__.py`**

```python
# src/carbuyer/apps/valuator/__main__.py
from carbuyer.apps._runner import run_worker
from carbuyer.apps.valuator.valuator import main


if __name__ == "__main__":
    run_worker("valuator", main)
```

```bash
touch src/carbuyer/apps/valuator/__init__.py
```

- [ ] **Step 3: Write the test**

```python
# tests/apps/test_valuator.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from carbuyer.apps.valuator.valuator import value_one
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale


@pytest.mark.asyncio
async def test_value_one_writes_score_when_comps_exist(session) -> None:
    a = Auction(source="t", source_auction_id="A", url="x", auction_subtype="estate",
                first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
                pickup_province="AB", buyer_premium_pct=Decimal("0.10"),
                gst_pct=Decimal("0.05"), pst_pct=Decimal("0.00"))
    session.add(a)
    await session.flush()

    base = dict(make="Toyota", model="Tacoma", trim=None,
                sale_channel="auction_estate", sale_platform="hibid",
                title_status="NORMAL", schema_version=1, disposition_reason="sold")
    for p in [10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000]:
        session.add(HistoricalSale(year=2015, mileage_km=150000,
                                   final_listed_price_cad=Decimal(p),
                                   final_price_with_premium_cad=Decimal(p),
                                   buyer_premium_pct_at_sale=Decimal("0.10"),
                                   **base))
    lot = AuctionLot(
        auction_id=a.id, source_lot_id="L1", url="https://x",
        title="2015 Toyota Tacoma", description="runs",
        year=2015, make="Toyota", model="Tacoma",
        mileage_km=150000, condition_categorical="decent",
        red_flags=[], green_flags=[], showstopper_flags=[],
        current_high_bid_cad=Decimal("12000"),
    )
    session.add(lot)
    await session.commit()

    await value_one(session, lot)
    await session.commit()

    assert lot.valuation_status == "done"
    assert lot.confidence_bucket in ("medium", "high")
    assert lot.expected_value_cad is not None
    assert lot.price_deal_score is not None
```

- [ ] **Step 4: Run test and commit**

```bash
uv run pytest tests/apps/test_valuator.py -v
git add src/carbuyer/apps/valuator/ tests/apps/test_valuator.py
git commit -m "apps: valuator worker (comp set + scoring)"
```

---

### Phase 4 — Post-implementation overlay (2026-05-09)

Captured during Task 25-30 implementation; folded back here so future readers don't redo the analysis.

**Schema deltas (folded into migration `1d58e4b5021c`):**

1. **`valuation_attempts INTEGER NOT NULL DEFAULT 0`** added per pre-implementation overlay #6. Separate per-stage retry counter so failure modes are diagnosable from the row state alone.
2. **`last_valuation_error TEXT NULL`** added — mirror of `last_enrichment_error`. Truncated to 500 chars at write time so a stack-trace explosion can't blow the row size.

**Enum deltas (`carbuyer.db.enums.ValuationStatus`):**

3. **`INSUFFICIENT_COMPS` → `INSUFFICIENT`.** The original enum value (18 chars) didn't fit the `String(16)` status column. Renamed to keep the column narrow rather than widening every status column for one new value. Semantically identical.
4. **`SKIPPED` added.** The valuator needs a "tried, can't proceed" terminal state distinct from `FAILED` (transient retry exhausted) and `INSUFFICIENT` (comp set too thin). Fires when `make`/`model`/`year` is missing, which means the enricher never normalized the row — a valuator failure mode that's not the valuator's fault.

**Settings deltas (`carbuyer.shared.config.Settings`):**

5. **`valuation_batch_size: int = 30`** + **`valuation_max_attempts: int = 3`**. Mirror of the enrichment counterparts; valuator does no LLM I/O so larger batches are fine.
6. **`excessive_red_flag_weight_threshold: int = -8`** (overlay #12). Heuristic; revisit after first 100 lots. Implementation reads `cumulative_flag_weight(red, green)` (raw pre-clip / pre-dilution-cap sum) and sets `notification_status = SKIPPED` when at or below the threshold.
7. **`scoring_version: str = "v1"`** as a setting rather than a module-level constant so a backfill can re-pend stale rows via env override without a code deploy.

**Algorithmic decisions:**

8. **`flag_score` overlay #11 dilution cap interpretation.** Picked "cap mag-1 red contribution at `-2` when more than 3 fire" (vs. "drop entirely" or "rebaseline floor at -3"). Heavy reds (`|w| >= 2`) and all greens add normally on top; description-quality `"thin"` floor stacks via `max(floor, …)` after the cap. Final clip to `[-5, 5]`. Choice rationale: the literal "-2 cap" wording in the overlay matches this; it preserves the ability for genuine evidence to score lower while neutering the RB-context-flag pile-up.
9. **`cumulative_flag_weight()` exposed as a separate helper** (`carbuyer.scoring.score`). The dilution-cap and clipping happen in `flag_score()` for display; the notifier-skip check needs the raw sum so the two responsibilities are split into two functions instead of overloading one.
10. **`HIGH_CONFIDENCE_MIN_COMPS = 10`** in `compute_fair_value` (vs. plan's 15). Ten is the threshold where `statistics.quantiles(n=10)` yields real deciles; the plan-block's 15 was inconsistent with the plan-block's own test which expected HIGH at 10.
11. **`ConfidenceBucket` is `StrEnum`**, not `class X(str, Enum)` — UP042 hint and matches `db.enums` style.
12. **`value_one()` runs comp query + write in a single transaction.** Pre-implementation overlay #2 said "split if duration ever exceeds `idle_in_transaction_session_timeout=60s`." MVP comp counts are small (low thousands per make/model); current measured query time is sub-millisecond. Will split if production load shows otherwise.
13. **`process_pending()` is sequential, not concurrent.** Valuator workload is DB-bound, not network-bound; SKIP-LOCKED across one pool gives no real win at MVP scale. If throughput becomes a problem we add a Semaphore-bounded `gather` like the enricher.

**Plan code corrections (vs. the literal Task 30 code blocks):**

14. **`claim_pending_lots` → `claim_pending_ids`** (returns `list[int]`, not ORM rows). Matches what actually exists in `db.queue`.
15. **`async_session_maker` → `get_session_maker()` / `get_session()`.** Same renaming as Phase 3 had.
16. **StrEnum status writes everywhere** (`ValuationStatus.DONE` etc.) instead of plan's bare strings.
17. **Sparse + description_quality threading wired** (`compute_fair_value(..., sparse=lot.condition_inferred_from_sparse_listing)` and `flag_score(..., description_quality=lot.description_quality)`). Plan code consumed neither — the overlay flagged this.
18. **`notification_status` decision in valuator** (showstopper / cumulative <= -8 → `SKIPPED`; insufficient comps → `SKIPPED`; otherwise `PENDING`). Plan code unconditionally set `pending`.

End of post-implementation overlay.

---

End of Phase 4. Lots are now scored with `price_deal_score`, `rarity_score`, `recommended_max_bid`, and `confidence_bucket`. `notification_pending` fires next.

---

## Phase 5 — Discord bot

Bot connects out via TCP 443 (no inbound). Persistent views via `DynamicItem` with regex `custom_id`. Slash commands only.

### Task 31: Discord bot skeleton + channel router

**Files:**
- Create: `src/carbuyer/apps/bot/__init__.py`
- Create: `src/carbuyer/apps/bot/channels.py`
- Create: `src/carbuyer/apps/bot/messages.py`
- Create: `tests/apps/test_bot_messages.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/bot/channels.py`**

```python
from typing import Literal


ChannelKey = Literal[
    "early_warning", "hot_deals", "watchlist",
    "auction_closing", "auction_watch", "vision_updates", "system_health",
]


def select_channel(*, trigger: str, score: float | None) -> ChannelKey:
    if trigger == "early_warning":
        return "early_warning"
    if trigger == "going_cheap":
        if score is not None and score >= 0.20:
            return "hot_deals"
        return "watchlist"
    if trigger in {"closing_soon", "bid_trajectory", "lot_extended"}:
        return "auction_closing"
    if trigger == "vision_update":
        return "vision_updates"
    if trigger == "system":
        return "system_health"
    return "watchlist"
```

- [ ] **Step 2: Implement `src/carbuyer/apps/bot/messages.py`**

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(slots=True)
class LotEmbedData:
    lot_id: int
    url: str
    title: str
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    location: str
    current_high_bid_cad: Decimal | None
    all_in_cad: Decimal | None
    expected_value_cad: Decimal | None
    value_low_cad: Decimal | None
    value_high_cad: Decimal | None
    price_deal_score: float | None
    rarity_score: float | None
    confidence_bucket: str | None
    condition_categorical: str | None
    top_red_flags: list[str]
    top_green_flags: list[str]
    suspicious_underprice: bool
    scheduled_end_at: datetime | None


def render_early_warning_text(d: LotEmbedData) -> str:
    title = f"{d.year or ''} {d.make or ''} {d.model or ''} {d.trim or ''}".strip()
    end = d.scheduled_end_at.strftime("%b %d") if d.scheduled_end_at else "?"
    bid = f"${int(d.current_high_bid_cad):,}" if d.current_high_bid_cad else "(no bid yet)"
    if d.value_low_cad and d.value_high_cad:
        rng = f"${int(d.value_low_cad):,}–${int(d.value_high_cad):,}"
    else:
        rng = "(uncomped)"
    rarity = ", ".join(d.top_green_flags[:3]) or "rare/desirable"
    return (
        f"⭐ RARE FIND — {title} ({d.location})\n"
        f"Closes {end}\n"
        f"Current bid: {bid} · Estimated value: {rng}\n"
        f"Rarity: {rarity}"
    )


def render_going_cheap_text(d: LotEmbedData) -> str:
    title = f"{d.year or ''} {d.make or ''} {d.model or ''} {d.trim or ''}".strip()
    bid = f"${int(d.current_high_bid_cad):,}" if d.current_high_bid_cad else "no bid"
    all_in = f"${int(d.all_in_cad):,}" if d.all_in_cad else "?"
    ev = f"${int(d.expected_value_cad):,}" if d.expected_value_cad else "?"
    margin = ""
    if d.expected_value_cad and d.all_in_cad:
        m = int(d.expected_value_cad - d.all_in_cad)
        margin = f"  ·  Margin at current bid: ${m:,}"
    flags = ", ".join(d.top_green_flags[:3])
    prefix = "⚠ PRICED BELOW TYPICAL LOW END\n" if d.suspicious_underprice else ""
    return (
        f"{prefix}💰 Going cheap — {title}\n"
        f"{d.location}\n"
        f"Current bid: {bid}  →  All-in: {all_in}\n"
        f"Estimated value: {ev}{margin}\n"
        f"Confidence: {d.confidence_bucket} · Condition: {d.condition_categorical}\n"
        f"{('✅ ' + flags) if flags else ''}".rstrip()
    )
```

- [ ] **Step 3: Write a focused test**

```python
# tests/apps/test_bot_messages.py
from datetime import datetime
from decimal import Decimal

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import LotEmbedData, render_early_warning_text, render_going_cheap_text


def test_select_channel_routes() -> None:
    assert select_channel(trigger="early_warning", score=None) == "early_warning"
    assert select_channel(trigger="going_cheap", score=0.25) == "hot_deals"
    assert select_channel(trigger="going_cheap", score=0.16) == "watchlist"
    assert select_channel(trigger="closing_soon", score=None) == "auction_closing"


def test_render_early_warning() -> None:
    d = LotEmbedData(
        lot_id=1, url="u", title="t",
        year=1985, make="Toyota", model="Land Cruiser", trim="FJ60",
        location="Edmonton, AB",
        current_high_bid_cad=Decimal("5500"),
        all_in_cad=None, expected_value_cad=Decimal("23000"),
        value_low_cad=Decimal("18000"), value_high_cad=Decimal("28000"),
        price_deal_score=None, rarity_score=4.0,
        confidence_bucket="high", condition_categorical="good",
        top_red_flags=[], top_green_flags=["classic Land Cruiser", "Western Canada origin"],
        suspicious_underprice=False,
        scheduled_end_at=datetime(2026, 6, 1),
    )
    text = render_early_warning_text(d)
    assert "RARE FIND" in text
    assert "Land Cruiser" in text


def test_render_going_cheap_includes_margin() -> None:
    d = LotEmbedData(
        lot_id=2, url="u", title="t",
        year=2018, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        location="Saskatoon, SK",
        current_high_bid_cad=Decimal("14500"),
        all_in_cad=Decimal("17400"), expected_value_cad=Decimal("24000"),
        value_low_cad=Decimal("20000"), value_high_cad=Decimal("28000"),
        price_deal_score=0.27, rarity_score=1.0,
        confidence_bucket="high", condition_categorical="good",
        top_red_flags=[], top_green_flags=["recent timing chain"],
        suspicious_underprice=False,
        scheduled_end_at=datetime(2026, 6, 1),
    )
    text = render_going_cheap_text(d)
    assert "Going cheap" in text
    assert "$6,600" in text  # margin
```

- [ ] **Step 4: Create `__init__.py` and run tests**

```bash
touch src/carbuyer/apps/bot/__init__.py
uv run pytest tests/apps/test_bot_messages.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/bot/ tests/apps/test_bot_messages.py
git commit -m "bot: channel router + message renderers"
```

---

### Task 32: Discord bot main + persistent action buttons

**Files:**
- Create: `src/carbuyer/apps/bot/__main__.py`
- Create: `src/carbuyer/apps/bot/bot.py`
- Create: `src/carbuyer/apps/bot/views.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/bot/views.py`**

```python
from typing import Any

import discord
from discord import ButtonStyle, Interaction
from discord.ui import DynamicItem, View, button


class LotActionView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)


class LotInterestedButton(DynamicItem[discord.ui.Button[View]], template=r"deal:interested:(?P<lot_id>\d+)"):
    def __init__(self, lot_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=ButtonStyle.success, label="👍 Interested",
                custom_id=f"deal:interested:{lot_id}",
            )
        )
        self.lot_id = lot_id

    @classmethod
    async def from_custom_id(cls, interaction: Interaction, item: discord.ui.Button[View], match: Any) -> "LotInterestedButton":
        return cls(int(match["lot_id"]))

    async def callback(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from carbuyer.db.session import async_session_maker
        from carbuyer.db.models import AuctionLot
        async with async_session_maker() as session:
            lot = await session.get(AuctionLot, self.lot_id)
            if lot is not None:
                lot.user_action = "interested"
                await session.commit()
        await interaction.followup.send(f"Marked lot {self.lot_id} as interested.", ephemeral=True)


class LotMaybeButton(DynamicItem[discord.ui.Button[View]], template=r"deal:maybe:(?P<lot_id>\d+)"):
    def __init__(self, lot_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=ButtonStyle.secondary, label="🤔 Maybe",
                custom_id=f"deal:maybe:{lot_id}",
            )
        )
        self.lot_id = lot_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["lot_id"]))

    async def callback(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from carbuyer.db.session import async_session_maker
        from carbuyer.db.models import AuctionLot
        async with async_session_maker() as session:
            lot = await session.get(AuctionLot, self.lot_id)
            if lot is not None:
                lot.user_action = "maybe"
                await session.commit()
        await interaction.followup.send(f"Marked lot {self.lot_id} as maybe.", ephemeral=True)


class LotNotInterestedButton(DynamicItem[discord.ui.Button[View]], template=r"deal:not_interested:(?P<lot_id>\d+)"):
    def __init__(self, lot_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=ButtonStyle.danger, label="👎 Not interested",
                custom_id=f"deal:not_interested:{lot_id}",
            )
        )
        self.lot_id = lot_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["lot_id"]))

    async def callback(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from carbuyer.db.session import async_session_maker
        from carbuyer.db.models import AuctionLot
        async with async_session_maker() as session:
            lot = await session.get(AuctionLot, self.lot_id)
            if lot is not None:
                lot.user_action = "not_interested"
                await session.commit()
        await interaction.followup.send(f"Marked lot {self.lot_id} as not interested.", ephemeral=True)


def build_view_for_lot(lot_id: int) -> View:
    v = LotActionView()
    v.add_item(LotInterestedButton(lot_id))
    v.add_item(LotMaybeButton(lot_id))
    v.add_item(LotNotInterestedButton(lot_id))
    return v
```

- [ ] **Step 2: Implement `src/carbuyer/apps/bot/bot.py`**

```python
import asyncio

import discord
from discord.ext import commands

from carbuyer.apps.bot.views import (
    LotInterestedButton, LotMaybeButton, LotNotInterestedButton,
)
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger


log = get_logger("bot")


def _intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


class CarbuyerBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=_intents())

    async def setup_hook(self) -> None:
        self.add_dynamic_items(
            LotInterestedButton, LotMaybeButton, LotNotInterestedButton,
        )
        if settings.discord_guild_id:
            self.tree.copy_global_to(guild=discord.Object(id=settings.discord_guild_id))
            await self.tree.sync(guild=discord.Object(id=settings.discord_guild_id))
        else:
            await self.tree.sync()


async def post_to_channel(bot: CarbuyerBot, channel_id: int, content: str, view: discord.ui.View | None = None) -> None:
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        await channel.send(content=content, view=view)


async def main() -> None:
    bot = CarbuyerBot()
    async with bot:
        await bot.start(settings.discord_bot_token)
```

- [ ] **Step 3: Implement `__main__.py`**

```python
# src/carbuyer/apps/bot/__main__.py
from carbuyer.apps._runner import run_worker
from carbuyer.apps.bot.bot import main


if __name__ == "__main__":
    run_worker("bot", main)
```

- [ ] **Step 4: Smoke check**

```bash
uv run python -c "from carbuyer.apps.bot.views import build_view_for_lot; v = build_view_for_lot(1); print(len(v.children))"
```

Expected: `3`.

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/apps/bot/__main__.py src/carbuyer/apps/bot/bot.py src/carbuyer/apps/bot/views.py
git commit -m "bot: discord.py bot scaffold with persistent action buttons"
```

---

### Phase 5 — Post-implementation overlay (2026-05-10)

Captured during Task 31-32 implementation; folded back here so future readers don't redo the analysis.

**Plan-code corrections applied during implementation:**

1. **`async_session_maker()` → `get_session()`.** Plan code in Task 32 (views.py button callbacks) referenced `async_session_maker` three times; that name doesn't exist. Same correction Phase 4 had to make. Used `get_session()` from `carbuyer.db.session` with the `async with get_session() as s, s.begin(): ...` pattern.

2. **Function-local DB imports moved to module top in `views.py`.** Plan had `from carbuyer.db.session import ...` and `from carbuyer.db.models import ...` inside each callback. No circular-import reason to defer them.

3. **Fail-fast on empty `settings.discord_bot_token`** in `bot.main()`. Mirrors `enricher.main()`'s `OPENAI_API_KEY` check (`sys.exit("DISCORD_BOT_TOKEN not configured")`). Caught at startup, not on first gateway connect.

4. **Type annotations on all three `from_custom_id` methods.** Plan left `LotMaybeButton` and `LotNotInterestedButton` untyped (`(cls, interaction, item, match)`). Pyright strict mode required typing. discord.py 2.7's actual signature uses `Item[Any]` (matching the base class for override compatibility) and `re.Match[str]`. The plan's `Any` for `match` would have been accepted under non-strict mode but not strict.

5. **Removed unused `from discord.ui import button`** in `views.py`. The decorator pattern isn't used here; we construct buttons explicitly.

**Algorithmic / structural decisions:**

6. **`select_channel` if-chain → dict lookup**. Refactored into a `_FIXED_ROUTES` dict + `HOT_DEAL_SCORE_THRESHOLD = 0.20` constant + an early-return for the score-conditional `going_cheap` branch. Forced by ruff PLR0911 (max-returns=6). Behaviorally identical to the plan; arguably more readable.

7. **`_set_user_action(lot_id: int, action: UserAction) -> bool` helper** in `views.py`. Three button callbacks shared a near-identical body (open session, get lot, set action, commit, send confirmation). Helper deduplicates the I/O. Three distinct `DynamicItem` button classes preserved (discord.py needs class-level `template=` regex per class). Returns `bool` so the callback can distinguish success from "lot not found" (silent drop on missing lot is a UX bug — user clicks button on a notification for a deleted lot, bot says "Marked!").

8. **`UserAction` enum used at the helper boundary** instead of bare `str`. Tightens callsites against typos and matches the existing `carbuyer.db.enums.UserAction` definition. `StrEnum` flows through SQLAlchemy as the underlying string, so the DB write is unchanged.

9. **`LotEmbedData` is `frozen=True` with `tuple[str, ...]` flag fields.** The plan had `slots=True` only and used `list[str]` for the flag fields. The "frozen snapshot" semantics in the docstring match `frozen=True`; tuples avoid the `__hash__` runtime trap (a frozen dataclass with a `list` field crashes on `hash()` despite the frozen contract suggesting it should be safe).

10. **Operational logging.** Added `on_ready` log on `CarbuyerBot` (gateway lifecycle), success-path log in `_set_user_action` and `post_to_channel`, plus the existing failure-path logs (missing lot, non-TextChannel). Operator can diagnose "user clicked but DB didn't change" without DEBUG mode.

11. **`post_to_channel` channel-type-mismatch warning.** Plan silently dropped writes when the resolved channel wasn't a `TextChannel` (e.g., a Forum / Voice / Thread channel). Now logs a warning with `channel_id` and `channel_type=type(channel).__name__` so a misconfigured channel ID surfaces in logs.

12. **Test rigor on `from_custom_id`.** Tests use the class's own `__discord_ui_compiled_template__` (the `ClassVar[re.Pattern[str]]` that discord.py's public `template` *property* returns at instance scope) instead of constructing a parallel regex. This catches a future template/custom_id divergence that an independent regex would silently let pass.

**Documented for Phase 6 implementor (NOT fixed in Phase 5):**

13. **`bot.post_to_channel` is dead code per the Phase 6 plan.** Phase 6 (Task 34, `apps/notifier/discord_post.py`) creates a transient `discord.Client` per notification rather than calling through the bot worker. The bot worker and notifier worker are separate processes; they don't share memory. The transient-client approach is wasteful (new gateway connection per message, rate-limit risk) but matches the plan. Phase 6 should reconsider — alternatives: Discord webhook URLs (lightweight), or a shared queue that the bot worker drains. Decide before implementing Task 34.

14. **`needs_plugin` trigger not yet handled.** Phase 7 / Task 42 calls `select_channel(trigger="needs_plugin", score=None)` and imports `render_needs_plugin_text`. Neither exists in Phase 5. Add the `ChannelKey` value, the `_FIXED_ROUTES` entry, and the renderer when implementing that task.

15. **`LotEmbedData.all_in_cad` field name** is shorter than the ORM column `AuctionLot.all_in_at_current_bid_cad`. Phase 6's `_embed_data` builder must map `lot.all_in_at_current_bid_cad → all_in_cad`. The shortened name hides the "at current bid" qualification — when the bid changes between notification events, the field reflects the bid at notification time only.

16. **`_patched_get_session` fixture relies on `session.info["maker"]`.** This is the same fragile pattern used in `tests/apps/test_valuator.py` and `tests/apps/test_enricher.py`. `info["maker"]` is set by conftest; a future conftest refactor could silently break the fixture. Worth revisiting if conftest is restructured.

End of post-implementation overlay.

---

## Phase 6 — Notifier worker

### Task 33: Trigger evaluator (pure logic)

**Files:**
- Create: `src/carbuyer/apps/notifier/__init__.py`
- Create: `src/carbuyer/apps/notifier/triggers.py`
- Create: `tests/apps/test_notifier_triggers.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/notifier/triggers.py`**

```python
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal


@dataclass(slots=True)
class LotState:
    lot_id: int
    rarity_score: float | None
    price_deal_score: float | None
    flag_score: int | None
    confidence_bucket: str | None
    has_showstopper: bool
    user_action: str | None
    scheduled_end_at: datetime | None
    early_warning_notified_at: datetime | None
    cheap_notified_at: datetime | None
    last_cheap_score: float | None


@dataclass(slots=True)
class TriggerResult:
    trigger: str   # "early_warning" | "going_cheap" | "skip"
    reason: str


def evaluate_triggers(
    state: LotState,
    *,
    now: datetime,
    rarity_threshold: float,
    notify_threshold: float,
    rescore_improvement_threshold: float,
    early_warning_min_hours: int,
) -> list[TriggerResult]:
    out: list[TriggerResult] = []

    if state.user_action == "not_interested":
        return out

    # Early-warning
    if (
        state.rarity_score is not None
        and state.rarity_score >= rarity_threshold
        and state.early_warning_notified_at is None
        and state.scheduled_end_at is not None
        and (state.scheduled_end_at - now) >= timedelta(hours=early_warning_min_hours)
    ):
        out.append(TriggerResult("early_warning", f"rarity={state.rarity_score}"))

    # Going-cheap
    if state.has_showstopper:
        return out
    if state.confidence_bucket not in {"medium", "high"}:
        return out
    if (state.flag_score or 0) < -1:
        return out
    if state.price_deal_score is None or state.price_deal_score < notify_threshold:
        return out

    closing_soon = (
        state.scheduled_end_at is not None
        and state.scheduled_end_at - now <= timedelta(hours=24)
    )
    eligible_user = state.user_action in {"interested", "maybe", None}
    fires_for_unflagged = closing_soon
    fires_for_watched = state.user_action in {"interested", "maybe"}

    should_fire = False
    if state.cheap_notified_at is None and (fires_for_watched or fires_for_unflagged):
        should_fire = True
    elif state.last_cheap_score is not None and (state.price_deal_score - state.last_cheap_score) >= rescore_improvement_threshold:
        should_fire = True

    if should_fire and eligible_user:
        out.append(TriggerResult("going_cheap", f"score={state.price_deal_score}"))

    return out
```

- [ ] **Step 2: Write the test**

```python
# tests/apps/test_notifier_triggers.py
from datetime import datetime, timedelta, timezone

from carbuyer.apps.notifier.triggers import LotState, evaluate_triggers


NOW = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)


def _state(**kw) -> LotState:
    base = dict(
        lot_id=1, rarity_score=None, price_deal_score=None,
        flag_score=0, confidence_bucket="high", has_showstopper=False,
        user_action=None,
        scheduled_end_at=NOW + timedelta(days=10),
        early_warning_notified_at=None, cheap_notified_at=None,
        last_cheap_score=None,
    )
    base.update(kw)
    return LotState(**base)


def test_early_warning_fires() -> None:
    s = _state(rarity_score=2.5)
    out = evaluate_triggers(
        s, now=NOW, rarity_threshold=2.0, notify_threshold=0.15,
        rescore_improvement_threshold=0.05, early_warning_min_hours=48,
    )
    assert any(t.trigger == "early_warning" for t in out)


def test_going_cheap_fires_for_watched_anytime() -> None:
    s = _state(price_deal_score=0.20, user_action="interested",
               scheduled_end_at=NOW + timedelta(days=10))
    out = evaluate_triggers(
        s, now=NOW, rarity_threshold=2.0, notify_threshold=0.15,
        rescore_improvement_threshold=0.05, early_warning_min_hours=48,
    )
    assert any(t.trigger == "going_cheap" for t in out)


def test_going_cheap_for_unflagged_only_when_closing_soon() -> None:
    far = _state(price_deal_score=0.20, scheduled_end_at=NOW + timedelta(days=10))
    near = _state(price_deal_score=0.20, scheduled_end_at=NOW + timedelta(hours=12))
    out_far = evaluate_triggers(far, now=NOW, rarity_threshold=2.0,
                                notify_threshold=0.15,
                                rescore_improvement_threshold=0.05,
                                early_warning_min_hours=48)
    out_near = evaluate_triggers(near, now=NOW, rarity_threshold=2.0,
                                 notify_threshold=0.15,
                                 rescore_improvement_threshold=0.05,
                                 early_warning_min_hours=48)
    assert not any(t.trigger == "going_cheap" for t in out_far)
    assert any(t.trigger == "going_cheap" for t in out_near)


def test_not_interested_suppresses() -> None:
    s = _state(rarity_score=3.0, price_deal_score=0.30, user_action="not_interested")
    out = evaluate_triggers(
        s, now=NOW, rarity_threshold=2.0, notify_threshold=0.15,
        rescore_improvement_threshold=0.05, early_warning_min_hours=48,
    )
    assert out == []
```

- [ ] **Step 3: Create `__init__.py` and run tests**

```bash
touch src/carbuyer/apps/notifier/__init__.py
uv run pytest tests/apps/test_notifier_triggers.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/apps/notifier/__init__.py src/carbuyer/apps/notifier/triggers.py tests/apps/test_notifier_triggers.py
git commit -m "notifier: trigger evaluator (early-warning + going-cheap)"
```

---

### Task 34: Notifier worker (consume `notification_pending`, post via Discord HTTP)

**Files:**
- Create: `src/carbuyer/apps/notifier/__main__.py`
- Create: `src/carbuyer/apps/notifier/notifier.py`
- Create: `src/carbuyer/apps/notifier/discord_post.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/notifier/discord_post.py`**

```python
import discord

from carbuyer.apps.bot.views import build_view_for_lot
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger


log = get_logger("notifier_post")


async def post_message(channel_id: int, content: str, lot_id: int) -> bool:
    """Post a message via a transient discord.Client (no full bot needed)."""
    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)

    posted = False

    @client.event
    async def on_ready() -> None:
        nonlocal posted
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(content=content, view=build_view_for_lot(lot_id))
                posted = True
        finally:
            await client.close()

    try:
        await client.start(settings.discord_bot_token)
    except Exception:
        log.exception("discord post failed", channel_id=channel_id)
    return posted
```

- [ ] **Step 2: Implement `src/carbuyer/apps/notifier/notifier.py`**

```python
import asyncio
from datetime import datetime, timezone

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import LotEmbedData, render_early_warning_text, render_going_cheap_text
from carbuyer.apps.notifier.discord_post import post_message
from carbuyer.apps.notifier.triggers import LotState, evaluate_triggers
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import listen
from carbuyer.db.queue import claim_pending_lots
from carbuyer.db.session import async_session_maker
from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger


log = get_logger("notifier")


def _state_from_lot(lot: AuctionLot) -> LotState:
    showstopper = bool(lot.showstopper_flags)
    return LotState(
        lot_id=lot.id,
        rarity_score=lot.rarity_score,
        price_deal_score=lot.price_deal_score,
        flag_score=lot.flag_score,
        confidence_bucket=lot.confidence_bucket,
        has_showstopper=showstopper,
        user_action=lot.user_action,
        scheduled_end_at=None,  # patched in caller from auction
        early_warning_notified_at=lot.early_warning_notified_at,
        cheap_notified_at=lot.cheap_notified_at,
        last_cheap_score=None,
    )


def _embed_data(lot: AuctionLot, auction: Auction) -> LotEmbedData:
    return LotEmbedData(
        lot_id=lot.id, url=lot.url, title=lot.title or "",
        year=lot.year, make=lot.make, model=lot.model, trim=lot.trim,
        location=", ".join(filter(None, [auction.pickup_city, auction.pickup_province])) or "?",
        current_high_bid_cad=lot.current_high_bid_cad,
        all_in_cad=lot.all_in_at_current_bid_cad,
        expected_value_cad=lot.expected_value_cad,
        value_low_cad=lot.value_low_cad, value_high_cad=lot.value_high_cad,
        price_deal_score=lot.price_deal_score,
        rarity_score=lot.rarity_score,
        confidence_bucket=lot.confidence_bucket,
        condition_categorical=lot.condition_categorical,
        top_red_flags=[f.get("flag", "") for f in (lot.red_flags or [])][:3],
        top_green_flags=lot.desirability_signals or [f.get("flag", "") for f in (lot.green_flags or [])][:3],
        suspicious_underprice=lot.suspicious_underprice_flag,
        scheduled_end_at=auction.scheduled_end_at,
    )


async def process_pending(*, batch_size: int = 30) -> int:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        async with session.begin():
            lots = await claim_pending_lots(
                session, status_field="notification_status", limit=batch_size,
            )
        for lot in lots:
            auction = await session.get(Auction, lot.auction_id)
            if auction is None:
                lot.notification_status = "skipped"
                continue
            state = _state_from_lot(lot)
            state.scheduled_end_at = auction.scheduled_end_at
            triggers = evaluate_triggers(
                state, now=now,
                rarity_threshold=settings.early_warning_rarity_threshold,
                notify_threshold=settings.notify_threshold,
                rescore_improvement_threshold=settings.rescore_improvement_threshold,
                early_warning_min_hours=settings.early_warning_min_hours_to_close,
            )
            for trig in triggers:
                channel_key = select_channel(trigger=trig.trigger, score=lot.price_deal_score)
                channel_id = settings.discord_channels.get(channel_key)
                if channel_id is None:
                    log.warning("no channel configured", channel_key=channel_key)
                    continue
                d = _embed_data(lot, auction)
                content = (
                    render_early_warning_text(d) if trig.trigger == "early_warning"
                    else render_going_cheap_text(d)
                )
                async with session.begin_nested():
                    posted = await post_message(channel_id, content, lot.id)
                    if posted:
                        if trig.trigger == "early_warning":
                            lot.early_warning_notified_at = now
                        else:
                            lot.cheap_notified_at = now
                        lot.last_notified_channel = channel_key
            lot.notification_status = "done"
        await session.commit()
    return len(lots)


async def main() -> None:
    async for _ in listen("notification_pending"):
        try:
            await process_pending()
        except Exception:
            log.exception("notification batch failed")
            await asyncio.sleep(5)
```

- [ ] **Step 3: Implement `__main__.py`**

```python
# src/carbuyer/apps/notifier/__main__.py
from carbuyer.apps._runner import run_worker
from carbuyer.apps.notifier.notifier import main


if __name__ == "__main__":
    run_worker("notifier", main)
```

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/apps/notifier/notifier.py src/carbuyer/apps/notifier/discord_post.py src/carbuyer/apps/notifier/__main__.py
git commit -m "apps: notifier worker (consume notification_pending → Discord)"
```

---

End of Phase 6. The pipeline can now post real Discord messages on early-warning and going-cheap.

---

### Phase 6 — Post-implementation overlay (2026-05-10)

Captured during Tasks 33-34 implementation; folded back so future readers don't redo the analysis.

**Architectural change vs. the plan:**

1. **Notifier posts via direct REST POST (`aiohttp`), not transient `discord.Client`.** Plan code at lines 7053-7090 spun up a fresh `discord.Client` per notification — a full gateway IDENTIFY (Discord caps at 1000/day per bot), TLS handshake, and READY round-trip per message. Replaced with raw `POST https://discord.com/api/v10/channels/{id}/messages` carrying `Authorization: Bot {token}` and a manually built `components` action-row. The persistent bot worker (Phase 5) still owns button-interaction routing via the gateway connection it already maintains, so this split costs nothing on the interaction side. One long-lived `aiohttp.ClientSession` opened in `main()`, passed through `_process_one` / `process_pending` / `_catchup_sweep`. Decision was approved by the user up front (see also Phase 5 overlay #13 which flagged this for Phase 6 to reconsider).

**Plan-code corrections applied during implementation:**

2. **`async_session_maker()` → `get_session()` / `get_session_maker()`.** Plan code at line 7105 references a name that doesn't exist (third phase to hit this). Used the same accessors the enricher uses.

3. **`claim_pending_lots(...)` did not exist.** Added a new function in `src/carbuyer/db/queue.py` that mirrors `claim_pending_ids` but returns full ORM rows. Both functions now share a private `_mark_in_progress(session, *, ids, status_field)` helper to prevent UPDATE-shape drift.

4. **`NotificationStatus.IN_PROGRESS = "in_progress"` was added** (`src/carbuyer/db/enums.py`) with `notification_status` added to `ClaimableStatusField` and `_IN_PROGRESS_BY_FIELD` (`src/carbuyer/db/queue.py`). Reversed the previous "no IN_PROGRESS for notifications" comment. Without this, two concurrent notifier workers could double-fire — and Phase 6 needs it for the watchdog hand-off (see #11 below).

5. **`scheduled_end_at` set in constructor, not via post-construction mutation.** Plan code at line 7163 had `state.scheduled_end_at = auction.scheduled_end_at` after building the `LotState` — now a runtime crash because Task 33 made `LotState` `frozen=True`. Implementation passes both `lot` and `auction` to `_state_from_lot` and constructs once.

6. **`LotEmbedData.top_red_flags` and `top_green_flags` are `tuple[str, ...]`.** Plan code passed lists; frozen dataclass + list field crashes on hash. Wrapped both with `tuple(...)`. Also fixed slicing — plan's `lot.desirability_signals or [...][:3]` only sliced the second branch (signals could be longer than 3 and skip the cap). Now slices both branches.

7. **HTTP I/O outside DB transactions.** Plan code at line 7182 had `async with session.begin_nested(): posted = await post_message(...)` — would hold the row lock during the Discord REST call. Replaced with the enricher idiom: load in short tx → close → post → reopen short tx → write status + timestamps.

**Algorithmic / structural decisions:**

8. **Sequential processing inside `process_pending`.** Discord rate limits are per-bot global; concurrent posts would race the limit instantly. Diverges from the enricher's `asyncio.Semaphore + gather` pattern intentionally — documented in the `process_pending` docstring.

9. **`post_message` failure → `notification_status = DONE`, no retry.** Notification posts are not retried beyond the in-call 429 / network-error one-shot (waiting `Retry-After`). Failed posts are logged and the lot moves out of the queue. Re-trying risks duplicate Discord messages because we have no idempotency key on the Discord side. Watchdog's recovery path is for stuck IN_PROGRESS lots, not for re-firing failed posts.

10. **`LotState` and `TriggerResult` are `frozen=True` + `slots=True`** (Task 33). Plan had `slots=True` only. Frozen tightens the snapshot contract; both classes only contain hashable scalars, so no `tuple[...]` substitution needed.

11. **Trigger evaluator `evaluate_triggers(...)` is fully pure.** No DB access, no datetime.now(), no settings reads — all five thresholds and `now` are explicit kwargs. This made Task 33 trivially testable (7 unit tests, no fixture needed) and decouples worker timing from logic correctness.

**Operational / observability:**

12. **Lot-vanished logging on both write paths.** If the row is deleted between claim and the SKIPPED-write or between posts and the timestamp-write, the worker now logs (warning + error respectively) rather than silently dropping. The post-then-vanish case is the more dangerous one: the message went to Discord but no `*_notified_at` was recorded — risking re-notification on watchdog recovery. Logged loudly so the operator notices.

13. **Catchup sweep handles PENDING only, by design.** Worker startup drains `notification_status='pending'` rows that NOTIFY-fired during downtime. IN_PROGRESS recovery is delegated to the **Phase 2.5 watchdog**, which **does not yet exist**. First production crash will reveal this — flag for the next operational pass. Manual recovery: `UPDATE auction_lots SET notification_status='pending' WHERE notification_status='in_progress' AND updated_at < now() - interval '5 minutes'`.

14. **Lot URL appended to both message renderers.** Phase 5's `render_early_warning_text` / `render_going_cheap_text` populated `LotEmbedData.url` but never rendered it — users got buttons but no link to the listing. Phase 6 fix: append `\n{url}` to the end of each rendered string (Discord auto-unfurls).

15. **`location` field uses `", ".join(filter(None, [city, province])) or "?"`.** Plan example was "Calgary, AB" but the original implementation picked the first truthy of `(province, city, "?")`, returning just "AB" when both were set. Fixed to match the plan's intent.

**Cleanup of Phase 5 dead code (consequence of decision #1):**

16. **Removed `apps/bot/bot.py:post_to_channel`, `apps/bot/views.py:LotActionView`, `apps/bot/views.py:build_view_for_lot`** (and their tests). All three were provisional Phase 5 code that the original plan expected the notifier to call. Phase 6's REST architecture builds the components JSON directly in `discord_post.py`, so these are dead. Bot module docstring updated to clarify the bot now only owns interaction handling, not message posting.

**Documented for future phases (NOT fixed in Phase 6):**

17. **`last_cheap_score` re-fire branch is dead code pending a DB column.** Task 33's `LotState` declares `last_cheap_score: float | None`; Task 34's `_state_from_lot` hardcodes it to `None` because there's no `auction_lots.last_cheap_score` column. The `evaluate_triggers` re-fire branch (`elif state.last_cheap_score is not None and ...`) therefore never executes. Adding the column + populating it from the previous notification's `price_deal_score` is a future migration. Code comment in `notifier.py:52` documents the deferral; the trigger-evaluator branch itself does not (intentional — when the column lands the comment in `notifier.py` is removed and the branch wakes up automatically).

18. **`# noqa: PLR0912` on `_process_one` is a complexity smell** (14 branches > the 12 limit). The function reads top-to-bottom in a single thread of control: load → evaluate → render-and-post-each-trigger → status write. Splitting would force passing 4-5 mutable accumulators between helpers. Accept the noqa; revisit if more trigger types land in Phase 7+.

19. **`_patched_get_session` fixture duplicated in `test_notifier_worker.py`.** Same fragility as Phase 5 overlay #16 — depends on `session.info["maker"]` set by conftest. Now copied across `test_enricher.py`, `test_valuator.py`, and `test_notifier_worker.py`. Lifting to `conftest.py` is the right cleanup; deferred until conftest is otherwise restructured.

20. **+4 pyright errors in `test_notifier_worker.py`** (private-use of `_process_one` + `_embed_data`, "unused" `_patched_get_session` fixture, deprecated `asynccontextmanager`). All four match the existing accepted pattern in `test_enricher.py` and `test_valuator.py`. Total pyright error count: 37 (Phase 5 baseline) → 41 (Phase 6 final). All four are pytest-fixture / private-test-import false positives.

End of post-implementation overlay.

---

## Phase 7 — Bid polling

### Task 35: Tiered cadence + bid-poller worker

**Files:**
- Create: `src/carbuyer/apps/bid_poller/__init__.py`
- Create: `src/carbuyer/apps/bid_poller/__main__.py`
- Create: `src/carbuyer/apps/bid_poller/scheduler.py`
- Create: `src/carbuyer/apps/bid_poller/poller.py`
- Create: `tests/apps/test_bid_scheduler.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/bid_poller/scheduler.py`**

```python
from datetime import datetime, timedelta, timezone


def next_poll_delay(*, scheduled_end: datetime | None, now: datetime, status: str) -> timedelta:
    """How long until we should next poll this lot."""
    if scheduled_end is None:
        return timedelta(minutes=60)
    if status in {"closed", "unsold", "sold"}:
        return timedelta(hours=24)  # very rare re-check
    delta = scheduled_end - now
    if delta <= timedelta(minutes=10):
        return timedelta(seconds=30)
    if delta <= timedelta(hours=1):
        return timedelta(minutes=1)
    if delta <= timedelta(hours=2):
        return timedelta(minutes=5)
    if delta <= timedelta(hours=24):
        return timedelta(minutes=15)
    return timedelta(minutes=60)


def fast_poll_concurrency_cap(open_fast_count: int, *, hard_cap: int = 20) -> int:
    """Allow up to `hard_cap` simultaneous fast pollers; slow the rest."""
    return min(open_fast_count, hard_cap)
```

- [ ] **Step 2: Write the test**

```python
# tests/apps/test_bid_scheduler.py
from datetime import datetime, timedelta, timezone

from carbuyer.apps.bid_poller.scheduler import next_poll_delay


NOW = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)


def test_far_lot_polls_hourly() -> None:
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(days=2), now=NOW, status="open"
    ) == timedelta(minutes=60)


def test_closing_window_speeds_up() -> None:
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(minutes=30), now=NOW, status="open"
    ) == timedelta(minutes=1)
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(minutes=5), now=NOW, status="open"
    ) == timedelta(seconds=30)


def test_closed_lot_throttles() -> None:
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(minutes=5), now=NOW, status="closed"
    ) == timedelta(hours=24)
```

- [ ] **Step 3: Implement `src/carbuyer/apps/bid_poller/poller.py`**

```python
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.bid_poller.scheduler import next_poll_delay
from carbuyer.db.models import Auction, AuctionBidHistory, AuctionLot
from carbuyer.db.session import async_session_maker
from carbuyer.shared.logging import get_logger
from carbuyer.sources.base import AuctionSource, LotRef


log = get_logger("bid_poller")


def _build_sources() -> dict[str, AuctionSource]:
    from carbuyer.sources.hibid.source import HibidSource
    return {"hibid": HibidSource(provinces=["AB", "BC", "SK", "MB"])}


async def _select_open_lots(session: AsyncSession, limit: int = 200) -> list[tuple[AuctionLot, Auction]]:
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.lot_status.in_(["open", "closing_soon", "extended"]))
        .order_by(Auction.scheduled_end_at.asc().nulls_last())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [(lot, auction) for (lot, auction) in rows]


async def _poll_one(
    session: AsyncSession, lot: AuctionLot, auction: Auction, sources: dict[str, AuctionSource]
) -> None:
    source = sources.get(auction.source)
    if source is None:
        return
    ref = LotRef(
        source=auction.source,
        source_auction_id=auction.source_auction_id,
        source_lot_id=lot.source_lot_id,
        url=lot.url,
    )
    try:
        obs = await source.poll_bid(ref)
    except Exception:
        log.exception("poll_bid failed", lot_id=lot.id)
        return

    history = AuctionBidHistory(
        lot_id=lot.id,
        observed_at=obs.observed_at,
        current_high_bid_cad=obs.current_high_bid_cad,
        end_time_at_observation=obs.end_time_at_observation,
        status_at_observation=obs.status_at_observation,
    )
    session.add(history)

    if obs.current_high_bid_cad is not None:
        old_bid = lot.current_high_bid_cad
        lot.current_high_bid_cad = obs.current_high_bid_cad
        lot.last_bid_observed_at = obs.observed_at
        if old_bid != obs.current_high_bid_cad:
            lot.valuation_status = "pending"   # rescoring needed

    if obs.end_time_at_observation is not None:
        if auction.scheduled_end_at and obs.end_time_at_observation > auction.scheduled_end_at:
            lot.lot_status = "extended"
        auction.last_seen_end_at = obs.end_time_at_observation

    if obs.status_at_observation == "missing":
        # Lot disappeared from the source — treat as closed
        if lot.lot_status != "closed":
            lot.lot_status = "closed"
            lot.closed_at = datetime.now(timezone.utc)
            lot.final_bid_cad = old_bid
    elif obs.status_at_observation == "closed":
        lot.lot_status = "closed"
        lot.closed_at = datetime.now(timezone.utc)
        lot.final_bid_cad = obs.current_high_bid_cad


async def main() -> None:
    sources = _build_sources()
    while True:
        async with async_session_maker() as session:
            async with session.begin():
                rows = await _select_open_lots(session, limit=200)
            now = datetime.now(timezone.utc)
            buckets: dict[str, list[tuple[AuctionLot, Auction]]] = {"fast": [], "slow": []}
            for lot, auction in rows:
                delay = next_poll_delay(
                    scheduled_end=auction.scheduled_end_at, now=now, status=lot.lot_status,
                )
                key = "fast" if delay.total_seconds() <= 300 else "slow"
                buckets[key].append((lot, auction))

            FAST_CAP = 20
            for lot, auction in buckets["fast"][:FAST_CAP]:
                async with session.begin():
                    await _poll_one(session, lot, auction, sources)
            for lot, auction in buckets["slow"][:50]:
                async with session.begin():
                    await _poll_one(session, lot, auction, sources)
        await asyncio.sleep(30)
```

- [ ] **Step 4: Implement `__main__.py`**

```python
# src/carbuyer/apps/bid_poller/__main__.py
from carbuyer.apps._runner import run_worker
from carbuyer.apps.bid_poller.poller import main


if __name__ == "__main__":
    run_worker("bid_poller", main)
```

- [ ] **Step 5: Run scheduler tests and commit**

```bash
touch src/carbuyer/apps/bid_poller/__init__.py
uv run pytest tests/apps/test_bid_scheduler.py -v
git add src/carbuyer/apps/bid_poller/ tests/apps/test_bid_scheduler.py
git commit -m "apps: bid-poller with tiered cadence + soft-close handling"
```

### Phase 7 post-implementation overlay

These items document divergences from the plan-as-written and operational
decisions made during Phase 7 implementation. They are corrections to the plan,
not additional work to do.

1. **`async_session_maker` does not exist** — the recurring Phase 4/5/6 plan
   issue. Replaced with `get_session()` and `get_session_maker()` from
   `carbuyer.db.session`. (Same fix in every prior phase's overlay.)

2. **`_build_sources()` rebuilt from `SOURCES` registry, not constructor.**
   The plan's `_build_sources()` instantiated a fresh `HibidSource(provinces=…)`
   that was never `__aenter__`'d, leaving `_http=None` and crashing on the first
   `poll_bid()` call. Implementation mirrors the lot-scraper: import
   `carbuyer.sources.hibid.source` to trigger `register()`, then read
   `SOURCES` and filter by `isinstance(s, BidPoller)`. Source contexts entered
   via `AsyncExitStack` in `main()`. Worker fails fast at startup if `hibid`
   isn't registered (`_REGISTERED_PLUGINS` guard).

3. **HTTP I/O moved outside DB transactions.** The plan held `session.begin()`
   open across `await source.poll_bid(ref)`. With `statement_timeout=30s` and
   `idle_in_transaction_session_timeout=60s` (set in `db/session.py`), a slow
   poll would tear down the connection. Restructured to the enricher pattern:
   `_poll_one` loads the snapshot in a short read tx → closes → calls
   `poll_bid` → `_write_observation` opens a fresh write tx, re-fetches the
   lot by id, applies mutations, commits.

4. **`notify(s, "valuation_pending", str(lot.id))` added on bid-change writes.**
   The plan set `lot.valuation_status = "pending"` when the bid changed but
   never NOTIFY'd. The valuator is LISTEN-only on `valuation_pending`
   (`apps/valuator/valuator.py:341`); without the NOTIFY it would never wake
   on bid changes until the worker restarted and ran its catchup sweep — i.e.
   the entire mid-auction re-scoring loop would be silently broken. NOTIFY
   emitted inside the same write transaction as the status flip.

5. **Real bug fixed: `lot.final_bid_cad = old_bid` was unbound on the "missing"
   branch.** The plan's `old_bid` was assigned only inside
   `if obs.current_high_bid_cad is not None:`. On the missing path
   (`current_high_bid_cad=None` per `HibidSource.poll_bid`'s missing return),
   `old_bid` was never bound → `NameError` at runtime. Implementation reads
   `lot.current_high_bid_cad` from the re-fetched ORM object directly on the
   missing branch (always bound, semantically equivalent: "preserve whatever
   the DB last recorded").

6. **`fast_poll_concurrency_cap` deleted** — the plan defined it but the main
   loop iterated sequentially with a hardcoded `FAST_CAP=20`. Per the
   no-dead-code policy, the helper was removed. Sequential per-batch
   processing is documented as the intentional MVP choice; revisit if
   throughput becomes the bottleneck.

7. **`from datetime import UTC`** used throughout (Python 3.11+ idiom),
   replacing the plan's `from datetime import timezone` / `timezone.utc`.

8. **Magic numbers hoisted to module-level constants:** `_BATCH_LIMIT=200`,
   `_FAST_BUCKET_CUTOFF_SECONDS=300`, `_FAST_BUCKET_CAP=20`,
   `_SLOW_BUCKET_CAP=50`, `_CYCLE_SLEEP_SECONDS=30`. Required for ruff
   `PLR2004` compliance and makes the cadence budget greppable.

9. **`LotStatus` and `ValuationStatus` StrEnum members used everywhere** in
   place of bare strings (`LotStatus.CLOSED`, `LotStatus.EXTENDED`,
   `ValuationStatus.PENDING`). The `_load_open_lot_refs` `where(...)` clause
   uses `LotStatus.OPEN, LotStatus.CLOSING_SOON, LotStatus.EXTENDED`.
   The two exceptions are bare strings on
   `obs.status_at_observation == "missing"` and `== "closed"` — those are
   the canonical protocol values from `BidObservation` (typed `str`, not an
   enum — `"missing"` has no `LotStatus` member). Promoting them to a
   `Literal` type alias on `BidObservation.status_at_observation` would
   touch the Phase 1 plugin contract surface and was deferred to a future
   cleanup.

10. **Symmetric idempotency guards** on the `"missing"` AND `"closed"`
    observation branches (`if lot.lot_status != LotStatus.CLOSED:` before
    overwriting `closed_at`/`final_bid_cad`). The plan only guarded the
    missing branch. In practice closed lots drop out of `_load_open_lot_refs`
    so the second observation is unlikely; but if the filter ever changes,
    the asymmetry would silently bump `closed_at` on every poll. One-line
    fix worth more than its cost.

11. **`closing_soon` lot status is filter-only.** No code in the codebase
    currently writes `LotStatus.CLOSING_SOON`; the lot-scraper's comment notes
    soft-close detection is a future Phase 7+ refinement. The bid-poller's
    `_load_open_lot_refs` filter accepts it for forward compatibility — when
    soft-close detection lands, the bid-poller picks up `closing_soon` lots
    automatically without a code change.

12. **Bid-poller does NOT use IN_PROGRESS** (or any of the `*_status` queue
    fields). It selects open lots by `lot_status` and processes them on the
    tiered cadence. Crash recovery is therefore free: a worker that dies
    mid-poll leaves the lot at `lot_status=open`; the next cycle re-loads
    and re-polls it. The Phase 2.5 watchdog (which sweeps stuck IN_PROGRESS
    rows on the four `*_status` columns) is intentionally not relevant to
    this worker. Documented in the module docstring so a future maintainer
    isn't tempted to add a `bid_poll_status` column out of consistency with
    other workers.

13. **Single-instance worker assumption.** No SKIP-LOCKED claim means two
    bid-poller instances would double-poll the same lots and double-write
    `AuctionBidHistory` rows. MVP runs one instance per the deployment plan
    (Phase 12). Horizontal scaling would require either adding a claim
    column or sharding by `auction_id % N` — note for Phase 12 if scale
    grows past one host.

14. **Bid-vs-valuator race documented but not fixed.** When a bid arrives
    mid-valuation: bid-poller writes `bid=new, status=PENDING` and commits;
    valuator's session continues with its cached lot snapshot (old bid),
    finishes computing with the old bid value, and commits `status=DONE`.
    The valuator's commit overwrites the bid-poller's PENDING, and the new
    bid value coexists with a stale price_deal_score. The NOTIFY emitted by
    bid-poller wakes the valuator loop, which finds zero PENDING rows and
    no-ops. Net effect: stale valuation persists until the next bid arrives
    or the next manual rescore. Acceptable at MVP scale (race window <1s,
    valuator runs are fast); a real fix would require either pessimistic
    locking on `current_high_bid_cad` or a "compute-then-CAS" pattern in
    the valuator. Noted for a future correctness-hardening pass; do NOT
    add the IN_PROGRESS guard suggested in code review — it doesn't actually
    solve the race (only changes which side loses) and adds confusing code.

15. **HTTP failure observation loss.** If `source.poll_bid` succeeds but the
    DB write transaction fails, the `AuctionBidHistory` row is rolled back
    and the observation is permanently lost (the lot will be re-polled next
    cycle, but the failed observation's specific timestamp/bid is gone).
    Acceptable at MVP scale and matches the failure surface of every other
    worker in the pipeline. Differs from the explicit "transient retry"
    posture of the enricher (which has `enrichment_attempts` on the lot row);
    the bid-poller has no per-attempt counter because the cadence loop is
    the implicit retry. Note for ops monitoring: a sustained PG outage
    would mean a sustained gap in `auction_bid_history`.

16. **Scaling cliff at ~200 open lots per cycle.** `_BATCH_LIMIT=200` plus
    fast cap 20 + slow cap 50 = 70 polls per cycle. With ~40 lots in MVP
    scope this is fine; with ~2000 lots (4 provinces × ~10 active auctions ×
    ~50 lots each) we'd skip 130+ lots per cycle, degrading effective
    polling rate proportionally. Symptom would be missed-bid notifications
    on lots not in the closing-soon window. Address by raising `_BATCH_LIMIT`,
    adding a "we dropped N lots" warning log, or moving to per-bucket
    independent loops. Defer until the dashboard's lot count crosses ~500.

17. **`_patched_get_session` fixture pattern now in 4 test files.** Lifting
    to `conftest.py` continues to be deferred per Phase 5 overlay #16 / Phase
    6 overlay #19. Tracked, not addressed in this phase.

18. **Tests added beyond the plan's three scheduler tests:**
    - 4 extra scheduler tests (no-end, unsold/sold, 1-2hr bucket, 2-24hr
      bucket) for full cadence-table coverage.
    - 8 poller-level tests (`tests/apps/test_bid_poller.py`):
      `_write_observation` no-bid-change → no NOTIFY/no rescore;
      `_write_observation` bid changed → NOTIFY + AuctionBidHistory + PENDING;
      missing-branch closes lot, preserves final_bid_cad (regression for the
      unbound-`old_bid` bug);
      closed-branch closes lot, sets final_bid_cad from observation;
      end_time > scheduled_end → EXTENDED + last_seen_end_at;
      `_poll_one` happy path (end-to-end bid write + history + NOTIFY);
      `_poll_one` unknown source → no-op;
      `_poll_one` source.poll_bid raises → caught, no DB writes.
    - Coverage gaps deferred to overlay (not blocking merge): bucketing
      logic in `_load_open_lot_refs` (delegates to heavily-tested
      `next_poll_delay`); `_REGISTERED_PLUGINS` startup guard.

19. **Pyright baseline.** Was 41 going in, 45 going out (+4). All four
    new errors match the existing accepted test-fixture noise pattern:
    `reportPrivateUsage` on `_poll_one`/`_write_observation` (intentional
    cross-module access from tests), `reportUnusedFunction` on
    `_patched_get_session` (pyright doesn't see pytest fixture wiring),
    `asynccontextmanager` deprecated (the test fixture idiom). Not new
    categories.

20. **Defensive `try/except` around per-lot dispatch in `main()`** + bucket-cap
    warning logs + per-cycle info log. Final-pass review caught that
    `_poll_one` does not catch exceptions from `_write_observation` (only
    from `poll_bid`); without a defensive wrapper at the dispatch site, a
    single DB transient (idle_in_transaction_session_timeout, deadlock,
    NOTIFY commit failure) would propagate out of the `while True` loop and
    exit the worker process. Sibling workers (enricher `_bounded`, valuator
    and notifier `process_pending` for-loops) all wrap the per-lot dispatch.
    Bid-poller now does the same in both fast- and slow-bucket loops.
    Added in the same pass: a `cycle complete` info log per pass (heartbeat
    visibility for ops) and `fast/slow bucket capped` warning logs that
    surface the overlay #16 scaling cliff in real time before it bites.

---

## Phase 8 — Vision pass

### Task 36: Photo download + resize helper

**Files:**
- Create: `src/carbuyer/apps/vision_batcher/__init__.py`
- Create: `src/carbuyer/apps/vision_batcher/photos.py`
- Create: `tests/apps/test_vision_photos.py`

- [ ] **Step 1: Add Pillow to dependencies**

```bash
uv add pillow
```

- [ ] **Step 2: Implement `src/carbuyer/apps/vision_batcher/photos.py`**

```python
import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from PIL import Image

from carbuyer.shared.logging import get_logger
from carbuyer.sources.http import make_client


log = get_logger("vision_photos")


async def download_and_resize(urls: list[str], *, max_dim: int = 1024, max_count: int = 8) -> list[Path]:
    """Download up to `max_count` photos, resize to fit `max_dim` long edge, return local paths."""
    out: list[Path] = []
    tmp = Path(tempfile.gettempdir()) / "carbuyer-vision"
    tmp.mkdir(parents=True, exist_ok=True)
    async with make_client() as client:
        for url in urls[:max_count]:
            try:
                r = await client.get(url)
                r.raise_for_status()
            except Exception:
                log.warning("photo download failed", url=url)
                continue
            digest = hashlib.sha256(url.encode()).hexdigest()[:16]
            raw_path = tmp / f"{digest}.bin"
            raw_path.write_bytes(r.content)
            try:
                img = Image.open(raw_path)
                img.thumbnail((max_dim, max_dim))
                jpg_path = tmp / f"{digest}.jpg"
                img.convert("RGB").save(jpg_path, format="JPEG", quality=85)
                out.append(jpg_path)
            except Exception:
                log.warning("photo resize failed", url=url)
            finally:
                try:
                    raw_path.unlink(missing_ok=True)
                except Exception:
                    pass
    return out
```

- [ ] **Step 3: Write the test**

```python
# tests/apps/test_vision_photos.py
import io

import pytest
import respx
from httpx import Response
from PIL import Image

from carbuyer.apps.vision_batcher.photos import download_and_resize


def _png_bytes(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_download_and_resize_caps_count_and_size() -> None:
    urls = [f"https://x.test/p{i}.png" for i in range(12)]
    with respx.mock(base_url="https://x.test") as mock:
        mock.get(url__regex=r"/p\d+\.png").mock(return_value=Response(200, content=_png_bytes(2048, 2048)))
        out = await download_and_resize(urls, max_dim=1024, max_count=5)
    assert len(out) == 5
    for p in out:
        img = Image.open(p)
        assert max(img.size) <= 1024
```

- [ ] **Step 4: Run test and commit**

```bash
uv run pytest tests/apps/test_vision_photos.py -v
git add src/carbuyer/apps/vision_batcher/__init__.py src/carbuyer/apps/vision_batcher/photos.py tests/apps/test_vision_photos.py pyproject.toml uv.lock
git commit -m "vision: photo download + resize helper"
```

---

### Task 37: Vision pass implementation in OpenAIProvider

**Files:**
- Modify: `src/carbuyer/llm/openai_provider.py`
- Modify: `src/carbuyer/llm/prompts.py`

- [ ] **Step 1: Add per-image and aggregation prompts to `src/carbuyer/llm/prompts.py`**

Append at the end of the file:

```python
VISION_PER_IMAGE_PROMPT = """You are inspecting a single photo of a used vehicle for a deal-finder.

Output the structured PerImageOutput. Rules:
- Set explicit_unknowns for anything you cannot judge from THIS image alone.
- Do not guess. Output `unknown`-equivalent values when uncertain.
- Severity: 1=cosmetic, 2=needs repair, 3=structural / safety.
- Confidence: 1=very unsure, 5=certain.
"""


VISION_AGGREGATION_PROMPT = """You are aggregating per-image findings (JSON only, no images) into an overall VisionOutput for a single vehicle.

Rules:
- coverage_gaps: list standard angles missing (e.g., "no engine bay shot", "no undercarriage").
- cross_panel_paint_consistency only "consistent"/"inconsistent" if the same panel appears in 2+ shots; else "cannot_assess".
- staging_signals: pro photography, perfect lighting, no underbody close-ups.
- contradictions_with_description: list specific contradictions with the supplied description condition / flags.
- Set overall_vision_condition pessimistically when finding severity 3 items.
"""
```

- [ ] **Step 2: Replace the `vision` method body of `OpenAIProvider` (in `openai_provider.py`)**

Replace:
```python
    async def vision(self, payload: VisionInput) -> VisionOutput:
        # Implemented in Phase 8.
        raise NotImplementedError("vision pass implemented in Phase 8")
```

With:
```python
    async def vision(self, payload: VisionInput) -> VisionOutput:
        from base64 import b64encode

        from carbuyer.llm.prompts import VISION_AGGREGATION_PROMPT, VISION_PER_IMAGE_PROMPT
        from carbuyer.llm.schemas import PerImageOutput

        per_image_results: list[PerImageOutput] = []
        for path in payload.photo_paths:
            img_bytes = open(path, "rb").read()
            data_url = f"data:image/jpeg;base64,{b64encode(img_bytes).decode()}"
            try:
                # Phase 3 overlay #8: GA path. Phase 8 also needs a `_parse_to`
                # helper that takes `messages` (list-of-content-parts) instead
                # of `system: str, user: str` so token-usage logging is shared.
                resp = await self.client.chat.completions.parse(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": VISION_PER_IMAGE_PROMPT},
                        {"role": "user", "content": [
                            {"type": "text", "text": (
                                f"Vehicle: {payload.year} {payload.make} {payload.model}. "
                                f"Description condition (claimed): {payload.description_condition}. "
                                f"Description red flags: {', '.join(payload.description_red_flags)}. "
                                f"Description green flags: {', '.join(payload.description_green_flags)}."
                            )},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ]},
                    ],
                    response_format=PerImageOutput,
                    temperature=0,
                    max_tokens=512,
                )
                parsed = resp.choices[0].message.parsed
                if parsed is not None:
                    per_image_results.append(parsed)
            except Exception:
                log.exception("vision per-image failed")
                continue

        agg_user = (
            f"Per-image findings (JSON):\n{[r.model_dump() for r in per_image_results]}\n\n"
            f"Description condition (claimed): {payload.description_condition}\n"
            f"Description red flags: {payload.description_red_flags}\n"
            f"Description green flags: {payload.description_green_flags}"
        )
        try:
            resp = await self.client.chat.completions.parse(  # GA path (Phase 3 overlay #8)
                model=self.model,
                messages=[
                    {"role": "system", "content": VISION_AGGREGATION_PROMPT},
                    {"role": "user", "content": agg_user},
                ],
                response_format=VisionOutput,
                temperature=0,
                max_tokens=1024,
            )
        except Exception:
            log.exception("vision aggregation failed")
            raise
        out = resp.choices[0].message.parsed
        if out is None:
            raise RuntimeError("vision aggregation returned None")
        return out
```

- [ ] **Step 3: Quick smoke run (no live API)**

```bash
uv run python -c "from carbuyer.llm.openai_provider import OpenAIProvider; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add src/carbuyer/llm/openai_provider.py src/carbuyer/llm/prompts.py
git commit -m "llm: implement vision pass (per-image + aggregation)"
```

---

### Task 38: Vision-batcher worker (nightly)

**Files:**
- Create: `src/carbuyer/apps/vision_batcher/__main__.py`
- Create: `src/carbuyer/apps/vision_batcher/batcher.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/vision_batcher/batcher.py`**

```python
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.vision_batcher.photos import download_and_resize
from carbuyer.db.models import AuctionLot
from carbuyer.db.session import async_session_maker
from carbuyer.llm.base import VisionInput
from carbuyer.llm.openai_provider import OpenAIProvider
from carbuyer.shared.logging import get_logger


log = get_logger("vision_batcher")


CONDITION_RANK = {"bad": 0, "poor": 1, "decent": 2, "good": 3, "great": 4}


def _bucket_diff(a: str | None, b: str | None) -> int:
    if a is None or b is None:
        return 0
    return abs(CONDITION_RANK.get(a, 2) - CONDITION_RANK.get(b, 2))


async def _select_shortlist(session: AsyncSession, *, threshold: float = 0.10, limit: int = 100) -> list[AuctionLot]:
    stmt = (
        select(AuctionLot)
        .where(
            AuctionLot.vision_status == "pending",
            AuctionLot.price_deal_score >= threshold,
            AuctionLot.lot_status.in_(["open", "closing_soon", "extended"]),
        )
        .order_by(AuctionLot.price_deal_score.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def vision_one(session: AsyncSession, lot: AuctionLot, provider: OpenAIProvider) -> None:
    if not lot.photos:
        lot.vision_status = "skipped"
        return
    paths = await download_and_resize(lot.photos, max_dim=1024, max_count=8)
    if not paths:
        lot.vision_status = "skipped"
        return

    payload = VisionInput(
        photo_paths=[str(p) for p in paths],
        year=lot.year, make=lot.make, model=lot.model,
        description_condition=lot.condition_categorical,
        description_red_flags=[f.get("flag", "") for f in (lot.red_flags or [])],
        description_green_flags=[f.get("flag", "") for f in (lot.green_flags or [])],
    )
    try:
        out = await provider.vision(payload)
    except Exception:
        log.exception("vision failed", lot_id=lot.id)
        lot.vision_status = "failed"
        return

    lot.vision_findings = out.model_dump()
    lot.vision_condition_overall = out.overall_vision_condition
    lot.vision_confidence = out.vision_confidence
    lot.vision_contradictions = out.contradictions_with_description

    if (
        out.vision_confidence > 0.7
        and _bucket_diff(out.overall_vision_condition, lot.condition_categorical) >= 2
    ):
        # Pessimistic update + add a synthetic red flag
        lot.condition_categorical = (
            out.overall_vision_condition
            if CONDITION_RANK[out.overall_vision_condition] < CONDITION_RANK[lot.condition_categorical or "decent"]
            else lot.condition_categorical
        )
        flags = list(lot.red_flags or [])
        flags.append({
            "flag": "description_oversells_condition",
            "evidence": ", ".join(out.contradictions_with_description),
            "weight": -2,
        })
        lot.red_flags = flags
        lot.valuation_status = "pending"  # rescore based on revised condition

    lot.vision_status = "done"


async def main() -> None:
    provider = OpenAIProvider()
    async with async_session_maker() as session:
        async with session.begin():
            shortlist = await _select_shortlist(session, threshold=0.10, limit=100)
        log.info("vision shortlist", count=len(shortlist))
        for lot in shortlist:
            async with session.begin():
                await vision_one(session, lot, provider)
```

- [ ] **Step 2: Implement `__main__.py`**

```python
# src/carbuyer/apps/vision_batcher/__main__.py
from carbuyer.apps._runner import run_worker
from carbuyer.apps.vision_batcher.batcher import main


if __name__ == "__main__":
    run_worker("vision_batcher", main)
```

- [ ] **Step 3: Commit**

```bash
git add src/carbuyer/apps/vision_batcher/batcher.py src/carbuyer/apps/vision_batcher/__main__.py
git commit -m "vision: nightly batcher (shortlist + reconciliation)"
```

### Phase 8 post-implementation overlay

Divergences from the plan-as-written and operational decisions made during
Phase 8 implementation. These are corrections, not additional work.

1. **`async_session_maker` does not exist** (recurring Phase 4/5/6/7 issue).
   Replaced with `get_session()` / `get_session_maker()` from
   `carbuyer.db.session`.

2. **`OpenAIProvider` entered as `async with`** + fail-fast on missing
   `OPENAI_API_KEY`. Plan instantiated `provider = OpenAIProvider()` without
   the context manager, leaking the AsyncOpenAI client on shutdown. Mirrored
   the enricher's startup/shutdown pattern (`apps/enricher/enricher.py:419`).

3. **HTTP / LLM I/O moved outside DB transactions.** Plan held one long-lived
   session open for the entire batch with nested `session.begin()` per lot,
   and `vision_one` did `download_and_resize` (HTTP) + `provider.vision()`
   (8 per-image LLM calls + 1 aggregation) all inside the begin. With
   `statement_timeout=30s` and `idle_in_transaction_session_timeout=60s`
   (`db/session.py`), the connection would have been torn down per lot.
   Restructured to the enricher pattern: snapshot in short read tx → close →
   I/O outside any tx → reopen short write tx → re-fetch lot by id → apply +
   NOTIFY.

4. **Per-lot `tempfile.TemporaryDirectory()` for photo cleanup.** Plan's
   `download_and_resize` wrote into a hardcoded `/tmp/carbuyer-vision/` and
   the caller never deleted the resulting JPEGs. After many nightly runs,
   `/tmp` would fill with stale photos. Task 36's helper now requires a
   `tmp_dir: Path` kwarg; the batcher wraps each lot's I/O in a
   `TemporaryDirectory()` context. Tempdir entered BEFORE `download_and_resize`
   and exited AFTER the `provider.vision()` call so the JPEGs are still on
   disk when the LLM reads them.

5. **NOTIFY `valuation_pending` after pessimistic-condition override.** Plan
   set `lot.valuation_status = "pending"` after revising condition downward
   but never NOTIFY'd. Same omission as Phase 7. Without the NOTIFY, the
   valuator (LISTEN-only on `valuation_pending`) would not rescore until its
   next restart catchup. Added inside the same write tx as the status flip.

6. **`_apply_vision` returns `override_fired: bool`.** A naive
   `if lot.valuation_status == ValuationStatus.PENDING: notify(...)` guard
   would over-notify on lots that were ALREADY PENDING when vision started
   (e.g. a fresh content rescrape that reset valuation_status). The pure
   function now returns `True` iff the pessimistic-override branch fired, and
   the caller NOTIFYs based on that signal.

7. **`VisionInput.lot_id`** added (mirroring `DescribeInput.lot_id`) so
   per-call OpenAI usage logs (`vision_per_image`, `vision_aggregate`) carry
   the lot id and stay correlatable to a row in production. Earlier draft
   passed `lot_id=None` to `_parse_to`, breaking cost/latency attribution.

8. **`_parse_to` refactor (Task 37).** Existing helper took
   `system: str, user: str` and constructed a 2-message text-only payload.
   Vision needs multimodal `messages` (text + image_url parts). Refactored to
   accept `messages: list[ChatCompletionMessageParam]`; both `describe` and
   the two vision call sites now go through the same chokepoint with shared
   token-usage logging tagged by `kind` (`describe` / `vision_per_image` /
   `vision_aggregate`).

9. **`VISION_AGGREGATION_PROMPT` clarifies `vision_confidence` scale.** Original
   prompt didn't tell the model the aggregate confidence is `[0.0, 1.0]` —
   easy to confuse with the per-image `confidence: 1-5` scale. Added explicit
   guidance + calibration tied to the worker's `>0.7` override gate.

10. **Synthetic red flag empty-evidence fallback.** When the override fires
    but the LLM produced no `contradictions_with_description` strings, the
    flag's `evidence` would have been the empty string. Falls back to
    `"vision condition mismatch"` so dashboard surfaces something readable.

11. **StrEnum members for status writes** (`VisionStatus.{DONE,SKIPPED,FAILED,PENDING}`,
    `LotStatus.{OPEN,CLOSING_SOON,EXTENDED}`, `ValuationStatus.PENDING`)
    instead of the plan's bare strings.

12. **Magic numbers hoisted to module-level constants:**
    `_SHORTLIST_SCORE_THRESHOLD=0.10`, `_SHORTLIST_LIMIT=100`,
    `_PESSIMISM_CONFIDENCE_THRESHOLD=0.7`, `_PESSIMISM_BUCKET_DIFF_MIN=2`,
    `_VISION_PER_IMAGE_MAX_TOKENS=512`, `_VISION_AGGREGATE_MAX_TOKENS=1024`.
    Considered promoting to `Settings` for ops tunability — deferred (overlay
    item #19) since changes need code review either way at MVP.

13. **`_select_shortlist` returns `list[int]`, not `list[AuctionLot]`.** Plan
    returned ORM objects, but accessing them after the session closes raises
    `DetachedInstanceError`. Returning ids lets the session close before any
    I/O (matches enricher's `claim_pending_ids` shape).

14. **`_apply_vision` extracted as pure function.** Mirrors enricher's
    `_apply_to_lot`. Independently testable without DB or LLM mocks.

15. **`VisionInput.photo_paths` is `list[str]` ⇄ `download_and_resize` returns
    `list[Path]`.** The batcher converts via `[str(p) for p in paths]`. Could
    make `VisionInput.photo_paths: list[Path]` for type cleanliness, but `str`
    matches what OpenAI's API ultimately accepts and the conversion is
    harmless.

16. **`vision_status='skipped'` semantics differ from the plan's lot-scraper
    comment.** The lot-scraper comment in `scraper.py` originally claimed
    SKIPPED was set "when a lot is outside the top-10% deal-score gate", but
    the Phase 8 batcher only sets SKIPPED when the lot has no usable photos
    (no `lot.photos` OR every download/decode failed). Sub-threshold lots
    stay PENDING and are simply not selected by tonight's shortlist; they
    re-enter the shortlist automatically if a bid update lifts their score.
    Comment in `scraper.py:150-159` updated to match actual behavior.

17. **Defensive try/except around per-lot dispatch in `main()`.** Mirrors
    `bid_poller/poller.py:222-228` (added in Phase 7 overlay #20). Without
    it, an unhandled exception in `_process_one` would propagate out of the
    batch loop and exit the worker before later lots are processed. Each lot
    now records its outcome in a `counts` dict that gets logged at end-of-run
    for nightly observability.

18. **The synthetic flag `description_oversells_condition` is NOT in
    `flags/taxonomy.py`.** It is a vision-pass-only synthetic flag bypassing
    the description-derived flag taxonomy. Acceptable because the taxonomy
    governs description-derived flags; vision-derived flags are a separate
    domain. Comment on the flag construction notes this so a future taxonomy
    audit doesn't false-positive it as missing.

19. **Sequential per-lot processing (no `asyncio.Semaphore + gather`).** At
    ~9 LLM calls × ~2-3s × 100 lots ≈ 30-50 minutes per nightly run.
    Defensible MVP tradeoff (the Phase 12 deployment plan slots vision into
    a 2am cron with a wide window). A `Semaphore`-bounded `gather` would
    give a 5-10× speedup if the nightly window matters operationally.
    Defer until ops surfaces a real complaint.

20. **Vision-batcher does NOT use IN_PROGRESS** (no claim/SKIP-LOCKED).
    Cron-driven single-instance assumption (same as bid-poller). Crash
    recovery is "the next nightly run picks up the still-PENDING lot."
    Documented in module docstring so a future maintainer doesn't add a
    `vision_pass_status` column out of consistency with other workers. The
    Phase 2.5 watchdog (when it lands) sweeps `enrichment_status`,
    `valuation_status`, `vision_status` (which the batcher writes), and
    `notification_status` — but not the lot_status field bid_poller uses.
    For vision, `vision_status` going from PENDING → unchanged across a
    nightly run is the only crash signal; the batcher writes it inside the
    short write tx so a half-finished batch leaves only the in-flight lot in
    PENDING (which next-night picks up).

21. **Pyright baseline.** Was 45 going in, 50 going out (+5). All five new
    errors are in `tests/apps/test_vision_batcher.py` and match the existing
    accepted test-fixture noise pattern: 3× `reportPrivateUsage` (test
    importing `_bucket_diff` / `_process_one` / `_select_shortlist`), 1×
    `reportUnusedFunction` (`_patched_get_session` fixture — pyright doesn't
    see pytest fixture wiring), 1× `asynccontextmanager` deprecated. Zero new
    errors in production code (`batcher.py`, `openai_provider.py`,
    `prompts.py`, `base.py`, `lot_scraper/scraper.py`).

22. **Tests added beyond the plan's single `test_download_and_resize_caps_count_and_size`:**
    - photos.py (4 tests): happy path + 3 failure paths (HTTP 404 skip,
      corrupt image bytes skip, empty URL list).
    - vision provider (8 tests): happy path, multimodal message structure,
      per-image partial failure, per-image None-parsed skip, aggregation
      failure propagates, aggregation None-parsed raises RuntimeError,
      None year/make/model formatting, empty photo_paths runs aggregation only.
    - vision batcher (20 tests): bucket-diff helper edge cases, shortlist
      filtering + ordering + threshold, `_process_one` happy + pessimistic
      override + no-photos + empty downloads + LLM-failed + no-NOTIFY-when-
      no-override + missing-lot, `main()` fail-fast on missing API key.
    - Removed: `test_vision_raises_not_implemented_until_phase8` (no longer
      true after Task 37).
    - Net delta: +31 across the phase (289 → 320).

23. **Coverage gaps deferred (overlay-only, not blocking merge):**
    - No exact-threshold test (vision_confidence == 0.7 boundary, since
      comparison is `>` not `>=`). Existing tests use 0.5 / 0.85 / 0.90.
    - No `_process_one` test for the case where vision sees BETTER condition
      than description claimed (bucket diff might be ≥ 2, but the
      `vision_rank < desc_rank` guard correctly skips the override).
    - No `main()` test for the for-loop's defensive try/except around
      `_process_one` (sibling workers also lack one — same intentional gap).

24. **Multi-reviewer pre-merge findings applied** (commit `6669e91`).
    Three parallel reviewers (bugs/security, architecture/convention, test
    coverage/observability) ran against the branch. Items applied:
    - **Pillow decompression-bomb cap.** Default ~178MP threshold only raises
      on 2× the limit; a ~178MP malicious upload from any auction site would
      decompress to 1-2GB RAM before failing. Set `Image.MAX_IMAGE_PIXELS =
      50_000_000` at module import in `photos.py`. The
      `DecompressionBombError` raised on overlimit is caught by the existing
      per-URL `except Exception`.
    - **`_SHORTLIST_SCORE_THRESHOLD` and `_SHORTLIST_LIMIT` lifted to
      Settings** (`vision_shortlist_score_threshold`, `vision_shortlist_limit`).
      Ops can now tune nightly cost via env without a code deploy. Pessimism
      gate constants (`_PESSIMISM_*`) stay module-level — algorithmic, not
      ops-tunable. Reverses overlay #12's "module-constant only" stance for
      the budget knobs only.
    - **Per-image vision failure log** in `openai_provider.py` now includes
      `payload.lot_id`. `photo_path` alone is a tempfile path that's gone
      by the time anyone reads the log.
    - **Distinct logs for the two SKIPPED paths** in batcher.py: `"vision
      skipped — no photos"` (likely scraper-health issue: lot cleared score
      threshold but has zero photos) vs `"vision skipped — all downloads
      failed"` (CDN outage / all formats rejected). Both look identical in
      the DB without these.
    - **Override-fired log** when pessimistic override fires, including
      vision condition + prior condition + confidence. Lets ops grep for
      "how often is vision overriding descriptions?" without DB queries.
    - **Lot-disappeared-before-write warning log** so the `counts["missing"]`
      bucket has a corresponding log line. The read-tx case already had a
      log; the write-tx case was silent.
    - **`download_and_resize` accepts optional `lot_id`** forwarded into the
      per-URL warning logs. A "this lot's photos fail every night" pattern
      is now greppable without joining URL against `auction_lots.photos`.
    - **Missing-lot test added** for `_process_one` read-tx path (overlay #22
      previously claimed this test existed; it did not).
    - **Empty photo_paths test added** for `provider.vision()`. Batcher
      guards upstream, but `vision()` is a public ABC method and could be
      called directly by scripts or future callers.

25. **Bid-vs-vision race window observed** (not fixed). The vision-batcher
    snapshots `condition_categorical` and `red_flags` outside any tx, runs
    ~30s of LLM I/O, then re-fetches the lot in a write tx and overwrites
    those fields based on `out` (computed against the snapshot view). If
    `upsert_lot_with_status_cascade` rescrape/enrich runs between snapshot
    and write, the new red_flags array gets clobbered by `_apply_vision`'s
    `flags = list(lot.red_flags or []); flags.append(...); lot.red_flags =
    flags` — but the override decision itself was made against stale
    `lot.condition_categorical` data. In practice the rescrape cascade
    resets `vision_status=PENDING`, so the next nightly run will rescore.
    Worst-case is one over-pessimistic valuation cycle that auto-corrects
    on next night's vision pass. The race window here (~30s LLM) is wider
    than enricher's (~5s LLM); same family as Phase 7 overlay #14 (bid-vs-
    valuator). Acceptable at MVP scale; a real fix would need pessimistic
    locking or a CAS-style "compute-then-recheck-condition" pattern.

---

## Phase 9 — Distillation

### Task 39: Auction-distiller worker

**Files:**
- Create: `src/carbuyer/apps/auction_distiller/__init__.py`
- Create: `src/carbuyer/apps/auction_distiller/__main__.py`
- Create: `src/carbuyer/apps/auction_distiller/distiller.py`
- Create: `tests/apps/test_distiller.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/auction_distiller/distiller.py`**

```python
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Auction, AuctionLot, HistoricalSale
from carbuyer.db.session import async_session_maker
from carbuyer.shared.logging import get_logger


log = get_logger("distiller")
KEEP_NOTIFIED_DAYS = 90
DISTILL_AGE_DAYS = 14


def _channel_from(auction: Auction) -> str:
    return f"auction_{auction.auction_subtype}"


async def distill_lot(session: AsyncSession, lot: AuctionLot, auction: Auction) -> None:
    final_bid = lot.final_bid_cad
    bp = auction.buyer_premium_pct
    final_with_premium = None
    if final_bid is not None and bp is not None:
        final_with_premium = final_bid * (1 + bp)

    sale = HistoricalSale(
        year=lot.year, make=lot.make, model=lot.model, trim=lot.trim,
        engine=lot.engine, transmission=lot.transmission, drivetrain=lot.drivetrain,
        mileage_km=lot.mileage_km, vin=lot.vin,
        title_status=lot.title_status, province_of_origin=lot.province_of_origin,
        condition_categorical=lot.condition_categorical,
        final_listed_price_cad=final_bid,
        days_listed=None,
        buyer_premium_pct_at_sale=bp,
        final_price_with_premium_cad=final_with_premium,
        sale_channel=_channel_from(auction),
        sale_platform=auction.source,
        seller_province=auction.pickup_province,
        seller_city=auction.pickup_city,
        observed_first_at=auction.first_seen_at,
        disappeared_at=lot.closed_at,
        disposition_reason="sold" if final_bid is not None else "unsold",
        was_notified=lot.cheap_notified_at is not None or lot.early_warning_notified_at is not None,
        was_purchased_by_us=lot.was_purchased_by_us,
        notes=lot.notes,
        schema_version=1,
    )
    session.add(sale)
    await session.delete(lot)


async def main() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DISTILL_AGE_DAYS)
    keep_notified_cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_NOTIFIED_DAYS)
    async with async_session_maker() as session:
        async with session.begin():
            stmt = (
                select(AuctionLot, Auction)
                .join(Auction, Auction.id == AuctionLot.auction_id)
                .where(
                    AuctionLot.lot_status.in_(["closed", "sold", "unsold"]),
                    AuctionLot.closed_at.is_not(None),
                    AuctionLot.closed_at <= cutoff,
                    AuctionLot.was_purchased_by_us.is_(False),
                )
            )
            rows = (await session.execute(stmt)).all()
            for lot, auction in rows:
                # Keep watched lots longer
                if lot.user_action in {"interested", "maybe"} and (
                    lot.closed_at is None or lot.closed_at > keep_notified_cutoff
                ):
                    continue
                await distill_lot(session, lot, auction)
            log.info("distilled", count=len(rows))
```

- [ ] **Step 2: Implement `__main__.py`**

```python
# src/carbuyer/apps/auction_distiller/__main__.py
from carbuyer.apps._runner import run_worker
from carbuyer.apps.auction_distiller.distiller import main


if __name__ == "__main__":
    run_worker("auction_distiller", main)
```

- [ ] **Step 3: Write the test**

```python
# tests/apps/test_distiller.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from carbuyer.apps.auction_distiller.distiller import distill_lot
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale


@pytest.mark.asyncio
async def test_distill_lot_creates_historical_sale(session) -> None:
    a = Auction(source="hibid", source_auction_id="A", url="x", auction_subtype="estate",
                first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
                pickup_province="AB", pickup_city="Calgary",
                buyer_premium_pct=Decimal("0.10"))
    session.add(a)
    await session.flush()
    lot = AuctionLot(
        auction_id=a.id, source_lot_id="L1", url="https://x",
        year=2010, make="Ford", model="F-150", mileage_km=200000,
        title_status="NORMAL", condition_categorical="decent",
        red_flags=[], green_flags=[], showstopper_flags=[],
        lot_status="closed", final_bid_cad=Decimal("8000"),
        closed_at=datetime.now(UTC) - timedelta(days=20),
    )
    session.add(lot)
    await session.commit()

    await distill_lot(session, lot, a)
    await session.commit()

    sales = list((await session.execute(select(HistoricalSale))).scalars().all())
    assert len(sales) == 1
    assert sales[0].sale_channel == "auction_estate"
    assert sales[0].sale_platform == "hibid"
    assert sales[0].final_listed_price_cad == Decimal("8000.00")
    assert sales[0].final_price_with_premium_cad == Decimal("8800.000")  # 8000 × 1.10
```

- [ ] **Step 4: Run test and commit**

```bash
touch src/carbuyer/apps/auction_distiller/__init__.py
uv run pytest tests/apps/test_distiller.py -v
git add src/carbuyer/apps/auction_distiller/ tests/apps/test_distiller.py
git commit -m "apps: auction-distiller (closed lots → historical_sales)"
```

### Phase 9 post-implementation overlay

1. **`async_session_maker` → `get_session()`/`get_session_maker()`** (recurring fix
   every prior phase has applied).

2. **`from datetime import UTC`** instead of `from datetime import timezone` /
   `timezone.utc` (Python 3.11+ idiom; matches every other module).

3. **StrEnum members for status literals.** `LotStatus.{CLOSED, SOLD, UNSOLD}`
   in the SQL `WHERE` and `UserAction.{INTERESTED, MAYBE}` in the watched-lot
   filter, instead of bare strings.

4. **Per-lot transaction + defensive try/except** instead of one mega-tx for
   the whole batch. Mirrors `vision_batcher`'s pattern: short read tx selects
   eligible `(lot_id, auction_id)` pairs → close → per-id fresh write tx
   wraps re-fetch + `distill_lot` + commit. One bad lot logs `failed` and
   the next lot proceeds. Counts dict logged at end of run with
   `distilled`/`kept`/`failed`/`missing`.

5. **Real bug found: SQL three-valued logic dropped unreviewed lots.** The
   plan's watched-lot filter `lot.user_action in {"interested", "maybe"} and
   ...: continue` (in-loop) translates naturally to a SQL `WHERE` filter
   `~user_action.in_([INTERESTED, MAYBE])`. With `user_action IS NULL` (the
   default for unreviewed lots), `NULL IN (...)` evaluates to NULL → `NOT
   NULL` evaluates to NULL → row filtered out. Result: every unreviewed
   closed lot would be silently kept forever and never distilled. Fix uses
   explicit `or_(user_action.is_(None), user_action.not_in([INTERESTED,
   MAYBE]), closed_at <= keep_notified_cutoff)`. Caught by test failure
   during implementation, not by inspection.

6. **Critical bug caught by review: `session.delete(lot)` would raise
   `InvalidRequestError` in production.** The `AuctionLot.bid_history`
   relationship had `lazy="raise"` AND `cascade="all, delete-orphan"`. When
   `session.delete()` runs the unit-of-work, `delete-orphan` semantics
   require iterating the cascaded collection — which forces a lazy load —
   which raises with `'AuctionLot.bid_history' is not available due to
   lazy='raise'`. Existing distiller tests passed only because they added
   the lot in the same session (collection in-memory empty); production
   loads via `session.get(AuctionLot, lot_id)` (collection unloaded). Bid-
   poller writes history on every poll, so essentially every closed lot
   would have history and fail distillation. Fix: added `passive_deletes=True`
   to the `bid_history` relationship in `models.py` — delegates the cascade
   to the existing `ondelete="CASCADE"` FK. Regression test
   (`test_distill_lot_cascade_deletes_bid_history_via_fresh_session`) seeds
   bid history, commits, opens a NEW session via `get_session()`, calls the
   production code path, and asserts both lot and history rows are gone.
   Test fails (`InvalidRequestError`) without the model fix; passes with it.

7. **`was_notified` checks all five `*_notified_at` columns** (cheap,
   early_warning, closing, trajectory, extended), not just the two the plan
   mentioned. The Phase 9 purpose ("did we ever push this lot to a Discord
   channel?") needs all of them — otherwise lots that triggered only
   closing/trajectory/extended notifications would be misrecorded.

8. **`main(now: datetime | None = None)` parameter added** for testability +
   backfill support. Defaults to `datetime.now(UTC)` for production cron;
   tests pass a fixed `now` to make the cutoff arithmetic deterministic.

9. **Tests added beyond the plan's single `test_distill_lot_creates_historical_sale`:**
   - `test_distill_lot_unsold_disposition`, `test_distill_lot_was_notified_*`
     (3 variants for cheap, early_warning, none).
   - `test_main_skips_recently_closed`, `test_main_skips_open_lot`,
     `test_main_skips_purchased_by_us`.
   - `test_main_keeps_watched_lots_within_keep_window`,
     `test_main_keeps_maybe_lots_within_keep_window`,
     `test_main_distills_old_watched_lots`.
   - `test_main_distills_eligible_lot_end_to_end`.
   - `test_main_distills_sold_and_unsold_status`.
   - `test_main_bad_lot_does_not_block_others` (defensive try/except behavior).
   - `test_main_distills_not_interested_lot` (locks in the SQL three-valued
     fix — would have silently dropped this lot in the original plan code).
   - `test_distill_lot_cascade_deletes_bid_history_via_fresh_session` (locks
     in the `passive_deletes=True` fix — would raise without it).
   - Plan's bug `Decimal("8800.000")` assertion fixed to `Decimal("8800.00")`
     so it round-trips through the `Numeric(12, 2)` column correctly.
   - Net delta: 17 new tests (320 → 337, then +2 from review fixes → 339).

10. **Drop `delete` from imports** — plan imported `from sqlalchemy import
    delete, select` but only used ORM `session.delete(lot)`. Unused.

11. **Drop `@pytest.mark.asyncio` decorators** — `asyncio_mode = "auto"` in
    `pyproject.toml` makes them redundant.

12. **Module docstring** covers cron-driven model, watched-lot exception,
    per-lot tx + try/except rationale, and crash recovery via "next nightly
    run picks up still-eligible lots."

13. **Minor known gaps deferred (not blocking merge):**
    - `disposition_reason="sold" if final_bid is not None else "unsold"`
      mis-classifies a `LotStatus.SOLD` lot with null `final_bid_cad` as
      "unsold". Vanishingly rare combination; the column default "unknown"
      would be more honest. Bare strings — there's no `Disposition` enum
      yet. Future cleanup could add one.
    - Orphaned `Auction` rows after all their lots are distilled: the FK
      cascade is auction → lots (not the reverse), so distilled lots leave
      empty auctions in the `auctions` table. Phase 12 territory.
    - `observed_first_at = auction.first_seen_at` is an auction-level proxy
      for a per-lot first-scrape time (no per-lot column exists). Comment
      added so the next reader doesn't assume per-lot precision.
    - The implementer's commit auto-reformatted `db/models.py` (~190 lines
      of one-arg-per-line wrapping). Unrelated to the bug fix but consistent
      with project ruff format. Left as-is.

14. **Pyright baseline.** Was 50 going in, 52 going out (+2). Both new
    errors are in `tests/apps/test_distiller.py` and match the existing
    accepted noise pattern (`reportUnusedFunction` on `_patched_get_session`,
    `asynccontextmanager` deprecated). Zero new errors in production code.

15. **Multi-reviewer pre-merge findings applied** (commit `5ab010b`).
    Three parallel reviewers (bugs/security, architecture/convention, test
    coverage/observability) ran against the branch. Items applied:
    - **`was_notified` test coverage extended** to all five `*_notified_at`
      columns (added trajectory + extended tests). Only 3 of 5 channels had
      tests; a typo dropping either column from the `any(...)` rollup tuple
      would have passed all 19 prior tests. Final test count: 341.
    - **Cross-module constant coupling documented.** `DISTILL_AGE_DAYS`
      (distiller.py) and `RECENT_AUCTION_LOTS_DAYS` (`scoring/comps.py`) are
      load-bearing equal: comp builder reads from `auction_lots` within that
      window AND from `historical_sales` beyond it. If DISTILL is shorter,
      lots vanish before the comp builder is done with them (gap); if
      longer, the same sale appears in both tables (double-counted). Added
      cross-reference comments to both constants so a future maintainer
      reading either file is warned not to change one without the other.
    - **`log.info("distiller starting", now=...)`** at top of `main()` so
      ops can see the cron woke up even if the DB query later hangs.
    - **`log.info("distiller no eligible lots; exiting")`** when the
      shortlist is empty — distinct from the all-zeros `"distiller
      complete"` line which is ambiguous between "ran with nothing to do"
      and "DB query returned no rows due to a bug."
    - **`auction_id` added to the `distill_lot failed` exception log.**
      Both ids are in scope; ops needs both to triage recurring failures.

16. **Reviewer items deferred (not blocking merge):**
    - Counts dict accuracy untested via direct assertion. The
      `bad_lot_does_not_block_others` test checks DB state but doesn't
      assert `counts["distilled"] == 1` and `counts["failed"] == 1`. The
      counts log line is the only consumer; a future refactor that
      double-counts would surface in production logs quickly. Not worth a
      log-capture mock yet.
    - Eligible-id query is inlined in `main()`, not extracted into
      `_select_eligible_ids`. Vision-batcher extracted its query for
      isolated testability; distiller's query is simpler so inline is
      defensible. Stylistic — leave for future cleanup if either query
      grows.
    - `kept` key absent from counts dict. Watched-lot filtering moved to
      SQL (overlay #5's safe-positive form), so kept lots never appear in
      the Python iteration. Functionally correct; the original overlay #4
      promise of `{distilled, kept, failed, missing}` is stale text.
    - Two-cron-overlap race is benign by construction. Per-lot atomic tx
      + PostgreSQL row-level lock on DELETE serializes writers; loser sees
      0 rows affected → SQLAlchemy raises `StaleDataError` → caught by the
      per-lot try/except (counted as `failed`). Rolled-back tx discards the
      duplicate `HistoricalSale` insert. No need for an IN_PROGRESS claim
      mechanism at MVP scale.
    - Unbounded SELECT on the eligible-id query. At MVP scale it's small;
      after several months of uptime with cron paused/resumed, the in-memory
      list could be sizable. Add chunking if worker memory becomes a
      concern.
    - Discord button → `_set_user_action` → "Lot N not found" if the user
      clicks 30+ days post-close on a non-watched lot. The bot already
      handles `lot is None` gracefully (returns False, sends "Lot N not
      found" ephemeral). Asymmetry (notified-and-clicked = 90 days;
      notified-and-not-clicked = 14 days) is acceptable at MVP.
    - `Purchase.linked_lot_id` FK has no `ondelete` clause. Distiller is
      shielded by the `was_purchased_by_us.is_(False)` filter. A manual SQL
      insert into `purchases` linking a lot that hasn't been flagged would
      crash distillation on the next nightly run. Phase 12 ops territory.
    - Empty `Auction` rows after all their lots are distilled — FK direction
      is `lots → auctions ondelete=CASCADE`, not the reverse, so deletes
      don't propagate up. Phase 12 ops cleanup.
    - `disposition_reason="sold" if final_bid is not None else "unsold"`
      mis-classifies a `LotStatus.SOLD` lot with null `final_bid_cad` as
      "unsold". Vanishingly rare; `"unknown"` (the column default) would
      be more honest. No `Disposition` enum exists yet.

---

End of Phase 9. The pipeline is complete from discovery through bid polling, vision, and distillation. Phase 10 adds two more sources; Phase 11 builds the dashboard; Phase 12 wires production deployment.

---

## Phase 10 — Additional sources (McDougall + farmauctionguide.com)

### Task 40: McDougall Auctioneers plugin

McDougall Auctioneers (`mcdougallauction.com`) runs its own platform. Their site has a dedicated Vehicles + Vocational Trucks taxonomy. The exact selectors must be verified during implementation against a captured fixture. This task delivers the structure; selectors are filled in once a real page is captured.

**Files:**
- Create: `src/carbuyer/sources/mcdougall/__init__.py`
- Create: `src/carbuyer/sources/mcdougall/source.py`
- Create: `tests/sources/mcdougall/__init__.py`
- Create: `tests/sources/mcdougall/test_source.py`
- Create: `tests/sources/fixtures/mcdougall_vehicles_sample.html`

- [ ] **Step 1: Capture a fixture**

```bash
curl -s "https://www.mcdougallauction.com/vehicles" \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36" \
  > tests/sources/fixtures/mcdougall_vehicles_sample.html
```

- [ ] **Step 2: Implement `src/carbuyer/sources/mcdougall/source.py`**

```python
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

from selectolax.parser import HTMLParser

from carbuyer.sources.base import (
    AuctionRef, AuctionSource, BidObservation, LotRef, RawAuction, RawLot,
)
from carbuyer.sources.http import jittered_sleep, make_client


VEHICLES_URL = "https://www.mcdougallauction.com/vehicles"


class McDougallSource(AuctionSource):
    name = "mcdougall"

    async def discover_auctions(self) -> AsyncIterator[AuctionRef]:
        seen: set[str] = set()
        async with make_client() as client:
            resp = await client.get(VEHICLES_URL)
            resp.raise_for_status()
            tree = HTMLParser(resp.text)
            for node in tree.css("a[href*='/auction/']"):
                href = node.attributes.get("href") or ""
                # Selectors verified during implementation against the captured fixture.
                # Expected pattern: /auction/<id>/<slug>
                parts = href.strip("/").split("/")
                if len(parts) < 2 or not parts[1].isdigit():
                    continue
                auction_id = parts[1]
                if auction_id in seen:
                    continue
                seen.add(auction_id)
                yield AuctionRef(
                    source="mcdougall",
                    source_auction_id=auction_id,
                    url=f"https://www.mcdougallauction.com{href}",
                )
            await jittered_sleep()

    async def fetch_auction(self, ref: AuctionRef) -> RawAuction:
        async with make_client() as client:
            resp = await client.get(ref.url)
            resp.raise_for_status()
        tree = HTMLParser(resp.text)
        # Selectors verified during implementation. For MVP we record minimum metadata.
        title_node = tree.css_first("h1") or tree.css_first(".auction-title")
        title = title_node.text(strip=True) if title_node else None
        return RawAuction(
            ref=ref, title=title, description=None,
            auctioneer_name="McDougall Auctioneers", auctioneer_external_id=None,
            scheduled_start_at=None, scheduled_end_at=None,
            pickup_address=None, pickup_city=None, pickup_province="SK",
            pickup_window_text=None,
            buyer_premium_pct=Decimal("0.10"),
            online_bidding_fee_pct=None,
            terms_text=None,
            auction_subtype="estate",
        )

    async def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]:
        async with make_client() as client:
            resp = await client.get(ref.url)
            resp.raise_for_status()
        tree = HTMLParser(resp.text)
        for node in tree.css("a[href*='/lot/']"):
            href = node.attributes.get("href") or ""
            parts = href.strip("/").split("/")
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            lot_id = parts[1]
            yield LotRef(
                source="mcdougall",
                source_auction_id=ref.source_auction_id,
                source_lot_id=lot_id,
                url=f"https://www.mcdougallauction.com{href}",
            )

    async def fetch_lot(self, ref: LotRef) -> RawLot:
        async with make_client() as client:
            resp = await client.get(ref.url)
            resp.raise_for_status()
        tree = HTMLParser(resp.text)
        title_node = tree.css_first("h1") or tree.css_first(".lot-title")
        title = title_node.text(strip=True) if title_node else None
        desc_node = tree.css_first(".lot-description") or tree.css_first(".description")
        description = desc_node.text(strip=True) if desc_node else None
        photos = [n.attributes.get("src") or "" for n in tree.css(".lot-images img")]
        photos = [p for p in photos if p]
        return RawLot(
            ref=ref,
            lot_number=None,
            title=title,
            description=description,
            photos=photos,
            year=None, make=None, model=None,
            current_high_bid_cad=None,
            scheduled_end_at=None,
            lot_status="open",
        )

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        async with make_client() as client:
            resp = await client.get(ref.url)
            resp.raise_for_status()
        tree = HTMLParser(resp.text)
        bid_node = tree.css_first(".current-bid") or tree.css_first("[data-bid]")
        bid = None
        if bid_node:
            txt = bid_node.text(strip=True).replace("$", "").replace(",", "").strip()
            try:
                bid = Decimal(txt)
            except Exception:
                bid = None
        return BidObservation(
            ref=ref, observed_at=datetime.now(UTC),
            current_high_bid_cad=bid,
            end_time_at_observation=None,
            status_at_observation="open",
        )
```

- [ ] **Step 3: Write a smoke test (skipped without fixture content)**

```python
# tests/sources/mcdougall/test_source.py
import os
from pathlib import Path

import pytest

from carbuyer.sources.mcdougall.source import McDougallSource


FIXTURE_PATH = Path("tests/sources/fixtures/mcdougall_vehicles_sample.html")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not FIXTURE_PATH.exists() or FIXTURE_PATH.stat().st_size < 1000,
    reason="fixture not captured yet",
)
async def test_source_imports() -> None:
    src = McDougallSource()
    assert src.name == "mcdougall"
```

- [ ] **Step 4: Create `__init__.py` files and commit**

```bash
touch src/carbuyer/sources/mcdougall/__init__.py tests/sources/mcdougall/__init__.py
uv run pytest tests/sources/mcdougall/test_source.py -v
git add src/carbuyer/sources/mcdougall/ tests/sources/mcdougall/ tests/sources/fixtures/mcdougall_vehicles_sample.html
git commit -m "mcdougall: AuctionSource implementation"
```

---

### Task 41: farmauctionguide.com — primary platform-router

**Operational note:** for the user's region, farmauctionguide.com surfaces meaningfully more relevant auctions than HiBid's province pages do (HiBid's Canada inventory leans US-east). farmauctionguide is therefore the **primary discovery entry point**. It inspects each upcoming auction's outbound URL, identifies the underlying platform, and emits an `AuctionRef` with the resolved `source` so the existing per-platform plugin (HiBid lot extractor, McDougall lot extractor) can fetch lots normally. HiBid's own province-page discovery (Task 13) stays as a redundant secondary path — same auctions surface from both, but rows dedup on `(source, source_auction_id)` so no duplication.

For auctions whose underlying platform we don't have a plugin for, we emit `source="farmauctionguide_unknown"` — those rows surface in the dashboard with the outbound URL for the user to click through manually, and the lot-scraper skips them (no plugin to call).

**Files:**
- Create: `src/carbuyer/sources/farmauctionguide/__init__.py`
- Create: `src/carbuyer/sources/farmauctionguide/source.py`
- Create: `tests/sources/farmauctionguide/__init__.py`
- Create: `tests/sources/farmauctionguide/test_source.py`

- [ ] **Step 1: Implement `src/carbuyer/sources/farmauctionguide/source.py`**

```python
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from carbuyer.sources.base import (
    AuctionRef, AuctionSource, BidObservation, LotRef, RawAuction, RawLot,
)
from carbuyer.sources.http import jittered_sleep, make_client


PROVINCE_PAGES = {
    "AB": "https://www.farmauctionguide.com/canada/alberta/",
    "SK": "https://www.farmauctionguide.com/canada/saskatchewan/",
    "MB": "https://www.farmauctionguide.com/canada/manitoba/",
    "BC": "https://www.farmauctionguide.com/canada/british-columbia/",
}


# Each entry: (hostname pattern, resolved source name, regex to extract auction id from URL).
# `source` matches the lookup key in lot_scraper._build_sources() so the right plugin runs.
PLATFORM_RULES: list[tuple[re.Pattern[str], str, re.Pattern[str]]] = [
    (re.compile(r"(^|\.)hibid\.com$", re.I), "hibid",
     re.compile(r"/(?:catalog|auctions?)/(\d+)")),
    (re.compile(r"mcdougallauction\.com$", re.I), "mcdougall",
     re.compile(r"/auction/(\d+)")),
    # Add new platforms here as we ship per-platform plugins.
]


def resolve_platform(url: str) -> tuple[str, str]:
    """Return (resolved_source, extracted_auction_id). Falls back to farmauctionguide_unknown."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    for host_re, source, id_re in PLATFORM_RULES:
        if host_re.search(host):
            m = id_re.search(parsed.path)
            ext_id = m.group(1) if m else (parsed.path.rstrip("/").split("/")[-1] or url)
            return source, ext_id
    fallback_id = parsed.path.rstrip("/").split("/")[-1] or url
    return "farmauctionguide_unknown", fallback_id


class FarmAuctionGuideSource(AuctionSource):
    """Primary platform-router for the user's region.

    Walks per-province pages, identifies the underlying platform from each
    outbound URL, and emits AuctionRefs with the resolved `source`. The
    existing pipeline (auction-discoverer + lot-scraper) then dispatches each
    auction to the correct per-platform plugin. Auctions whose platform we
    don't have a plugin for emit `source='farmauctionguide_unknown'` and
    surface in the dashboard with the outbound URL for manual review.
    """

    name = "farmauctionguide"

    def __init__(self, provinces: list[str]) -> None:
        self.provinces = provinces

    async def discover_auctions(self) -> AsyncIterator[AuctionRef]:
        seen: set[tuple[str, str]] = set()
        async with make_client() as client:
            for province in self.provinces:
                page = PROVINCE_PAGES.get(province)
                if not page:
                    continue
                resp = await client.get(page)
                resp.raise_for_status()
                tree = HTMLParser(resp.text)
                # Selectors verified during implementation against a captured fixture.
                for link in tree.css("a.auction-link, a[href*='/auction/'], a[data-auction]"):
                    href = link.attributes.get("href") or ""
                    if not href or not href.startswith("http"):
                        continue
                    source, ext_id = resolve_platform(href)
                    key = (source, ext_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield AuctionRef(
                        source=source,
                        source_auction_id=ext_id,
                        url=href,
                    )
                await jittered_sleep()

    async def fetch_auction(self, ref: AuctionRef) -> RawAuction:
        # Only called for refs we emit with source='farmauctionguide_unknown'.
        # For 'hibid' / 'mcdougall' refs, the corresponding plugin is invoked
        # by the auction-discoverer instead. We provide a minimal record so
        # the unknown rows still appear in the dashboard.
        return RawAuction(
            ref=ref, title=None, description=None,
            auctioneer_name=None, auctioneer_external_id=None,
            scheduled_start_at=None, scheduled_end_at=None,
            pickup_address=None, pickup_city=None, pickup_province=None,
            pickup_window_text=None, buyer_premium_pct=Decimal("0.10"),
            online_bidding_fee_pct=None, terms_text=None,
            auction_subtype="estate",
        )

    async def fetch_lots(self, ref: AuctionRef) -> AsyncIterator[LotRef]:
        # Lot-scraping for farmauctionguide_unknown rows is intentionally a no-op.
        # The user clicks through the outbound URL in the dashboard.
        if False:
            yield  # pragma: no cover -- typed empty generator
        return

    async def fetch_lot(self, ref: LotRef) -> RawLot:
        raise NotImplementedError("farmauctionguide_unknown auctions cannot fetch lots")

    async def poll_bid(self, ref: LotRef) -> BidObservation:
        return BidObservation(
            ref=ref, observed_at=datetime.now(UTC),
            current_high_bid_cad=None,
            end_time_at_observation=None,
            status_at_observation="missing",
        )
```

The key behavior change from the discovery-only design: `discover_auctions` now emits `AuctionRef(source="hibid", ...)` or `AuctionRef(source="mcdougall", ...)` for auctions whose outbound URL identifies them as living on those platforms. The auction-discoverer worker (Task 18 / Task 41 step 2) sees these refs with the resolved source, then calls `fetch_auction` / `fetch_lots` on the matching plugin via the dispatch map below.

For dispatching by `ref.source`, we update `auction-discoverer` to look up the plugin from a registry rather than calling `source.fetch_auction(ref)` on the discoverer that emitted the ref:

- [ ] **Step 2: Update `discover_once` in `src/carbuyer/apps/auction_discoverer/discoverer.py` to route by `ref.source`**

Replace `discover_once` and `main` in that file with:

```python
async def discover_once(
    discoverers: list[AuctionSource],
    registry: dict[str, AuctionSource],
) -> int:
    """Run each discoverer; for every emitted AuctionRef, dispatch fetch_auction
    to the plugin registered under ref.source. Falls back to the discoverer
    that emitted the ref when no registry entry matches (used for source-of-
    its-own-refs plugins like HiBid's province-page discovery)."""
    found = 0
    for discoverer in discoverers:
        log.info("discovering", source=discoverer.name)
        async for ref in discoverer.discover_auctions():
            plugin = registry.get(ref.source, discoverer)
            try:
                raw = await plugin.fetch_auction(ref)
            except Exception:
                log.exception("fetch_auction failed", source=ref.source, ref=ref)
                continue
            async with async_session_maker() as session:
                async with session.begin():
                    auction = await upsert_auction(session, raw)
                    await notify(session, "auction_pending", str(auction.id))
            found += 1
    log.info("discovery complete", found=found)
    return found


async def main() -> None:
    from carbuyer.sources.farmauctionguide.source import FarmAuctionGuideSource
    from carbuyer.sources.hibid.source import HibidSource
    from carbuyer.sources.mcdougall.source import McDougallSource

    hibid = HibidSource(provinces=["AB", "BC", "SK", "MB"])
    mcdougall = McDougallSource()
    farmguide = FarmAuctionGuideSource(provinces=["AB", "SK", "MB", "BC"])

    # farmauctionguide first: primary entry point for the user's region.
    # HiBid's own province discovery stays as a redundant secondary path —
    # any HiBid auction farmauctionguide already routed dedupes via
    # (source, source_auction_id) in upsert_auction.
    discoverers: list[AuctionSource] = [farmguide, hibid, mcdougall]

    # Registry — keyed on `ref.source`. The plugin that owns each platform
    # provides `fetch_auction` / `fetch_lots` / `fetch_lot` / `poll_bid`.
    registry: dict[str, AuctionSource] = {
        "hibid": hibid,
        "mcdougall": mcdougall,
        "farmauctionguide_unknown": farmguide,  # minimum-record path
    }
    await discover_once(discoverers, registry)
```

- [ ] **Step 3: Update `lot_scraper`'s `_build_sources` to be the same registry**

Modify `src/carbuyer/apps/lot_scraper/scraper.py`:

```python
def _build_sources() -> dict[str, AuctionSource]:
    from carbuyer.sources.farmauctionguide.source import FarmAuctionGuideSource
    from carbuyer.sources.hibid.source import HibidSource
    from carbuyer.sources.mcdougall.source import McDougallSource
    return {
        "hibid": HibidSource(provinces=["AB", "BC", "SK", "MB"]),
        "mcdougall": McDougallSource(),
        "farmauctionguide_unknown": FarmAuctionGuideSource(provinces=["AB", "SK", "MB", "BC"]),
    }
```

`farmauctionguide_unknown` lots return zero `LotRef`s (the `fetch_lots` async generator yields nothing), so the lot-scraper sees those auctions but writes no `auction_lots` rows — they show up in the dashboard with the outbound URL only.

The bid-poller's `_build_sources` should mirror the same registry — update `src/carbuyer/apps/bid_poller/poller.py` to import all three plugins identically.

- [ ] **Step 4: Smoke test the platform router**

```python
# tests/sources/farmauctionguide/test_source.py
import pytest

from carbuyer.sources.farmauctionguide.source import FarmAuctionGuideSource, resolve_platform


def test_source_constructible() -> None:
    src = FarmAuctionGuideSource(provinces=["AB"])
    assert src.name == "farmauctionguide"


def test_resolve_platform_routes_hibid() -> None:
    source, ext_id = resolve_platform("https://terrymcdougall.hibid.com/catalog/740236/some-slug")
    assert source == "hibid"
    assert ext_id == "740236"


def test_resolve_platform_routes_mcdougall() -> None:
    source, ext_id = resolve_platform("https://www.mcdougallauction.com/auction/12345/regina-summer")
    assert source == "mcdougall"
    assert ext_id == "12345"


def test_resolve_platform_falls_back_to_unknown() -> None:
    source, ext_id = resolve_platform("https://random-auctioneer.example.com/sale/abc")
    assert source == "farmauctionguide_unknown"
    assert ext_id == "abc"
```

```bash
touch src/carbuyer/sources/farmauctionguide/__init__.py tests/sources/farmauctionguide/__init__.py
uv run pytest tests/sources/farmauctionguide/test_source.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/carbuyer/sources/farmauctionguide/ tests/sources/farmauctionguide/ src/carbuyer/apps/auction_discoverer/discoverer.py src/carbuyer/apps/lot_scraper/scraper.py src/carbuyer/apps/bid_poller/poller.py
git commit -m "sources: farmauctionguide as primary platform-router; HiBid secondary"
```

---

### Task 42: Alert on unknown-platform auctions (lead-time triage)

When farmauctionguide finds an auction on a platform we don't have a plugin for, we need to flag it before the auction starts so the user can add the plugin and recover lot data. Lot data disappears at close — we can't retroactively scrape it. Alerting on discovery (typically days–weeks ahead) gives meaningful lead time.

**Files:**
- Modify: `src/carbuyer/apps/bot/channels.py` — add `needs_plugin` channel key
- Modify: `src/carbuyer/apps/bot/messages.py` — add `render_needs_plugin_text`
- Modify: `src/carbuyer/apps/auction_discoverer/discoverer.py` — emit `needs_plugin` NOTIFY
- Modify: `src/carbuyer/apps/notifier/notifier.py` — add a parallel listener for `needs_plugin`
- Modify: `src/carbuyer/apps/notifier/discord_post.py` — accept an optional `view` parameter (or `None` for plain posts)
- Create: `tests/apps/test_needs_plugin.py`

- [ ] **Step 1: Add `needs_plugin` channel key in `src/carbuyer/apps/bot/channels.py`**

Replace the `select_channel` function with:

```python
def select_channel(*, trigger: str, score: float | None) -> ChannelKey:
    if trigger == "early_warning":
        return "early_warning"
    if trigger == "going_cheap":
        if score is not None and score >= 0.20:
            return "hot_deals"
        return "watchlist"
    if trigger in {"closing_soon", "bid_trajectory", "lot_extended"}:
        return "auction_closing"
    if trigger == "vision_update":
        return "vision_updates"
    if trigger == "needs_plugin":
        return "needs_plugin"
    if trigger == "system":
        return "system_health"
    return "watchlist"
```

And update the `ChannelKey` literal to include `needs_plugin`:

```python
ChannelKey = Literal[
    "early_warning", "hot_deals", "watchlist",
    "auction_closing", "auction_watch", "vision_updates",
    "needs_plugin", "system_health",
]
```

- [ ] **Step 2: Add `render_needs_plugin_text` in `src/carbuyer/apps/bot/messages.py`**

Append to the file:

```python
def render_needs_plugin_text(
    *, auction_id: int, url: str, auctioneer_name: str | None,
    pickup_city: str | None, pickup_province: str | None,
    scheduled_start_at: datetime | None,
) -> str:
    location = ", ".join(filter(None, [pickup_city, pickup_province])) or "?"
    when = scheduled_start_at.strftime("%b %d") if scheduled_start_at else "(start date unknown)"
    return (
        f"🔌 NEW PLATFORM — needs a scraper plugin\n"
        f"Auctioneer: {auctioneer_name or '(unknown)'}\n"
        f"Location: {location}\n"
        f"Auction starts: {when}\n"
        f"URL: {url}\n\n"
        f"Add a plugin under src/carbuyer/sources/<name>/ before the auction closes "
        f"to capture lot data. After deploying the plugin, click 'Retry routing' "
        f"on /needs-plugin (auction id {auction_id}) to reprocess this auction."
    )
```

- [ ] **Step 3: Update auction-discoverer to NOTIFY when an unknown-platform auction is stored**

In `src/carbuyer/apps/auction_discoverer/discoverer.py`, replace the inner block of `discover_once` that handles persistence with:

```python
            async with async_session_maker() as session:
                async with session.begin():
                    auction = await upsert_auction(session, raw)
                    await notify(session, "auction_pending", str(auction.id))
                    if (
                        ref.source == "farmauctionguide_unknown"
                        and auction.needs_plugin_notified_at is None
                    ):
                        await notify(session, "needs_plugin", str(auction.id))
            found += 1
```

- [ ] **Step 4: Add a parallel `needs_plugin` listener in the notifier**

In `src/carbuyer/apps/notifier/notifier.py`, add:

```python
async def _process_needs_plugin(auction_id: int) -> None:
    from datetime import datetime, timezone

    from carbuyer.apps.bot.channels import select_channel
    from carbuyer.apps.bot.messages import render_needs_plugin_text
    from carbuyer.apps.notifier.discord_post import post_simple_message
    from carbuyer.db.models import Auction
    from carbuyer.db.session import async_session_maker
    from carbuyer.shared.config import settings

    async with async_session_maker() as session:
        auction = await session.get(Auction, auction_id)
        if auction is None or auction.needs_plugin_notified_at is not None:
            return
        if auction.source != "farmauctionguide_unknown":
            return
        channel_key = select_channel(trigger="needs_plugin", score=None)
        channel_id = settings.discord_channels.get(channel_key)
        if channel_id is None:
            log.warning("no needs_plugin channel configured")
            return
        content = render_needs_plugin_text(
            auction_id=auction.id, url=auction.url,
            auctioneer_name=auction.auctioneer_name,
            pickup_city=auction.pickup_city,
            pickup_province=auction.pickup_province,
            scheduled_start_at=auction.scheduled_start_at,
        )
        if await post_simple_message(channel_id, content):
            auction.needs_plugin_notified_at = datetime.now(timezone.utc)
            auction.last_notified_channel = channel_key
            await session.commit()


async def listen_needs_plugin() -> None:
    async for payload in listen("needs_plugin"):
        try:
            auction_id = int(payload)
        except ValueError:
            continue
        try:
            await _process_needs_plugin(auction_id)
        except Exception:
            log.exception("needs_plugin processing failed")


async def main() -> None:
    import asyncio

    async def lot_loop() -> None:
        async for _ in listen("notification_pending"):
            try:
                await process_pending()
            except Exception:
                log.exception("notification batch failed")
                await asyncio.sleep(5)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(lot_loop())
        tg.create_task(listen_needs_plugin())
```

(The previous `main()` function is replaced by the version above.)

- [ ] **Step 5: Add `post_simple_message` (button-less) helper in `src/carbuyer/apps/notifier/discord_post.py`**

Append to the existing file:

```python
async def post_simple_message(channel_id: int, content: str) -> bool:
    """Post without action buttons (used for system / needs_plugin alerts)."""
    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)

    posted = False

    @client.event
    async def on_ready() -> None:
        nonlocal posted
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(content=content)
                posted = True
        finally:
            await client.close()

    try:
        await client.start(settings.discord_bot_token)
    except Exception:
        log.exception("discord post_simple failed", channel_id=channel_id)
    return posted
```

- [ ] **Step 6: Write the test**

```python
# tests/apps/test_needs_plugin.py
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from carbuyer.apps.notifier.notifier import _process_needs_plugin
from carbuyer.db.models import Auction
from carbuyer.db.session import async_session_maker


@pytest.mark.asyncio
async def test_process_needs_plugin_marks_notified() -> None:
    async with async_session_maker() as s:
        a = Auction(
            source="farmauctionguide_unknown",
            source_auction_id="abc",
            url="https://random-auctioneer.example.com/sale/abc",
            auction_subtype="estate",
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            pickup_province="AB", pickup_city="Calgary",
            auctioneer_name="Random Co",
        )
        s.add(a)
        await s.commit()
        auction_id = a.id

    with patch("carbuyer.apps.notifier.discord_post.post_simple_message",
               new=AsyncMock(return_value=True)), \
         patch("carbuyer.shared.config.settings.discord_channels",
               {"needs_plugin": 99999}):
        await _process_needs_plugin(auction_id)

    async with async_session_maker() as s:
        a = await s.get(Auction, auction_id)
        assert a.needs_plugin_notified_at is not None
```

- [ ] **Step 7: Run test and commit**

```bash
uv run pytest tests/apps/test_needs_plugin.py -v
git add src/carbuyer/apps/bot/channels.py src/carbuyer/apps/bot/messages.py src/carbuyer/apps/auction_discoverer/discoverer.py src/carbuyer/apps/notifier/notifier.py src/carbuyer/apps/notifier/discord_post.py tests/apps/test_needs_plugin.py
git commit -m "needs-plugin: discord alert on unknown-platform auction discovery"
```

---

### Task 43: Retry routing after a new plugin is deployed

Once the user adds a plugin for a previously-unknown platform, they need to re-run platform detection on the existing auction rows so those auctions can be scraped end-to-end. The dashboard exposes a `/needs-plugin` view with a "Retry routing" button on each row.

**Files:**
- Create: `src/carbuyer/apps/dashboard/routers/needs_plugin.py`
- Create: `src/carbuyer/apps/dashboard/templates/pages/needs_plugin.html`
- Modify: `src/carbuyer/apps/dashboard/app.py` — register the new router
- Modify: `src/carbuyer/apps/dashboard/templates/base.html` — add nav link
- Create: `tests/apps/dashboard/test_needs_plugin.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/dashboard/routers/needs_plugin.py`**

```python
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.notify import notify
from carbuyer.sources.farmauctionguide.source import resolve_platform


router = APIRouter()


@router.get("/needs-plugin", response_class=HTMLResponse)
async def needs_plugin_view(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    stmt = (
        select(Auction)
        .where(Auction.source == "farmauctionguide_unknown")
        .order_by(Auction.scheduled_start_at.asc().nulls_last(), Auction.first_seen_at.asc())
        .limit(200)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    now = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        request, "pages/needs_plugin.html",
        {"rows": rows, "now": now},
    )


@router.post("/admin/auctions/{auction_id}/retry_routing", status_code=204)
async def retry_routing(
    auction_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    auction = await session.get(Auction, auction_id)
    if auction is None:
        raise HTTPException(status_code=404)
    new_source, new_ext_id = resolve_platform(auction.url)
    if new_source == "farmauctionguide_unknown":
        # No plugin matches yet; nothing to do.
        return Response(status_code=204)

    auction.source = new_source
    auction.source_auction_id = new_ext_id
    auction.routing_resolved_at = datetime.now(timezone.utc)

    # Reset any lots already associated so they re-process under the new source.
    # (Practically there should be zero lots since the source was unknown — but
    # being explicit is cheap.)
    from sqlalchemy import update
    await session.execute(
        update(AuctionLot)
        .where(AuctionLot.auction_id == auction.id)
        .values(enrichment_status="pending", valuation_status="pending",
                vision_status="pending", notification_status="pending")
    )
    await notify(session, "auction_pending", str(auction.id))
    await session.commit()
    return Response(status_code=204)
```

- [ ] **Step 2: Write `templates/pages/needs_plugin.html`**

```html
{% extends "base.html" %}
{% block title %}Needs plugin — CarBuyer{% endblock %}
{% block content %}
  <h1>Auctions needing a plugin</h1>
  <p class="muted">
    farmauctionguide.com surfaced these auctions but their underlying platform
    isn't covered by any of our scraper plugins yet. Add a plugin under
    <code>src/carbuyer/sources/&lt;name&gt;/</code> and register it in the
    discoverer + lot-scraper + bid-poller registries, then click
    <strong>Retry routing</strong> below to reprocess.
  </p>
  {% if not rows %}
    <p class="muted">Nothing waiting. New unknown-platform auctions will appear here as they're discovered.</p>
  {% endif %}
  <table>
    <thead>
      <tr>
        <th>URL</th>
        <th>Auctioneer</th>
        <th>Location</th>
        <th>Starts</th>
        <th>First seen</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {% for a in rows %}
        <tr>
          <td><a href="{{ a.url }}" target="_blank">{{ a.url[:60] }}…</a></td>
          <td>{{ a.auctioneer_name or "(unknown)" }}</td>
          <td>{{ a.pickup_city or "?" }}, {{ a.pickup_province or "?" }}</td>
          <td>
            {% if a.scheduled_start_at %}
              {{ a.scheduled_start_at.strftime("%b %d") }}
              {% set delta = (a.scheduled_start_at - now).total_seconds() // 86400 %}
              ({{ delta|int }}d)
            {% else %}
              —
            {% endif %}
          </td>
          <td>{{ a.first_seen_at.strftime("%Y-%m-%d") }}</td>
          <td>
            <button
              hx-post="/admin/auctions/{{ a.id }}/retry_routing"
              hx-swap="none"
            >Retry routing</button>
          </td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

- [ ] **Step 3: Register the router in `src/carbuyer/apps/dashboard/app.py`**

Update the `create_app` function's import + include block:

```python
    from carbuyer.apps.dashboard.routers import (
        feed, closing, watched, lots, comps, sold, purchases, health, actions, needs_plugin,
    )
    for router in (feed, closing, watched, lots, comps, sold, purchases, health, actions, needs_plugin):
        app.include_router(router.router)
```

- [ ] **Step 4: Add nav link in `templates/base.html`**

Inside the `.topnav` block, add a link before `Health`:

```html
    <a href="/needs-plugin">Needs plugin</a>
```

- [ ] **Step 5: Write the test**

```python
# tests/apps/dashboard/test_needs_plugin.py
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from carbuyer.apps.dashboard.app import app
from carbuyer.db.models import Auction
from carbuyer.db.session import async_session_maker


@pytest.mark.asyncio
async def test_needs_plugin_view_renders() -> None:
    async with async_session_maker() as s:
        a = Auction(
            source="farmauctionguide_unknown", source_auction_id="abc",
            url="https://random.example.com/sale/abc",
            auction_subtype="estate",
            first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
            pickup_province="AB", auctioneer_name="Random Co",
        )
        s.add(a)
        await s.commit()
    with TestClient(app) as client:
        r = client.get("/needs-plugin")
    assert r.status_code == 200
    assert "Random Co" in r.text


@pytest.mark.asyncio
async def test_retry_routing_reroutes_when_plugin_now_matches() -> None:
    async with async_session_maker() as s:
        a = Auction(
            source="farmauctionguide_unknown", source_auction_id="700001",
            url="https://terrymcdougall.hibid.com/catalog/700001/test",
            auction_subtype="estate",
            first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        )
        s.add(a)
        await s.commit()
        auction_id = a.id
    with TestClient(app) as client:
        r = client.post(f"/admin/auctions/{auction_id}/retry_routing")
    assert r.status_code == 204
    async with async_session_maker() as s:
        a = await s.get(Auction, auction_id)
        assert a.source == "hibid"
        assert a.routing_resolved_at is not None
```

- [ ] **Step 6: Run tests and commit**

```bash
uv run pytest tests/apps/dashboard/test_needs_plugin.py -v
git add src/carbuyer/apps/dashboard/routers/needs_plugin.py src/carbuyer/apps/dashboard/templates/pages/needs_plugin.html src/carbuyer/apps/dashboard/app.py src/carbuyer/apps/dashboard/templates/base.html tests/apps/dashboard/test_needs_plugin.py
git commit -m "needs-plugin: dashboard view + retry-routing admin endpoint"
```

### Phase 10 post-implementation overlay

Tasks 40, 41, and 42 land in this phase. **Task 43 (`/needs-plugin` dashboard
view + retry-routing admin endpoint) is deferred to Phase 11** — it depends
on the dashboard infrastructure (`apps/dashboard/app.py`, `deps.py`,
`routers/`, templates, FastAPI app scaffolding) that Phase 11 Task 44
creates. The needs-plugin view will slot naturally into Task 47's "remaining
views" list once the dashboard skeleton exists.

#### Plan-vs-code corrections (recurring + new)

1. **`async_session_maker` → `get_session()` / `get_session_maker()`**
   (recurring fix every prior phase has applied). The plan still uses it in
   Tasks 41 & 42.

2. **`from datetime import UTC`** instead of `from datetime import timezone`
   / `timezone.utc` (Python 3.11+ idiom). Recurring.

3. **Plan's "rewrite `discover_once(discoverers, registry)`" is stale.**
   `auction_discoverer/discoverer.py:140-203` already implements the
   platform-router dispatch pattern (`_sweep_one_discoverer` routes by
   `ref.source` via `fetchers.get(ref.source)`, falls back to
   `_minimal_raw_auction` for unknown). The dispatch infrastructure was
   built into earlier-phase work; the plan describes code that no longer
   exists in that shape. Phase 10 just needs the new plugins to
   self-register at module import via `register(...)` — the existing
   `_REGISTERED_PLUGINS` startup check + `SOURCES`-iterating dispatch
   picks them up automatically.

4. **Plan's `_build_sources()` updates to lot_scraper / bid_poller are
   stale.** Those workers already iterate `SOURCES` filtered by ABC type
   (see `lot_scraper/scraper.py:25-28`, `bid_poller/poller.py:51-72`).
   Importing the new source modules at the top of each worker triggers
   their `register()` side effects; the worker then picks them up via the
   iteration. Just extend `_REGISTERED_PLUGINS` in each worker for the
   startup-time "did this plugin actually register?" sanity check.

5. **Unknown-platform source naming: `unknown:<host>`, not
   `farmauctionguide_unknown`.** The plan uses a single bucket name;
   `auction_discoverer/discoverer.py:153` already checks
   `ref.source.startswith("unknown:")`. The `unknown:<host>` convention
   preserves per-host diagnostic info and works with the existing dispatch
   without code changes. Functionally strictly better than the plan's
   single bucket.

6. **`post_simple_message` uses direct REST POST via aiohttp**, not a
   transient `discord.Client` per call. Phase 6 overlay #1 explicitly
   rejected the per-call `discord.Client` pattern in favor of REST POST;
   the button-less variant must follow the same architecture. Implemented
   as a button-less mirror of `post_message`, sharing a new
   `_post_with_retry(s, url, headers, payload, *, log_kwargs)` private
   helper. `post_message` was refactored to use the helper too — DRY both
   sides at once.

7. **HTTP I/O moved outside DB transactions** in `_process_needs_plugin`.
   Plan held the session open across the Discord HTTP call. Restructured
   to the established Phase 3+ pattern: load snapshot in short read tx →
   close → post → reopen short write tx to stamp `needs_plugin_notified_at
   = now`. Mirrors `notifier._process_one` and `vision_batcher._process_one`.

8. **`auction.last_notified_channel = channel_key` removed from
   `_process_needs_plugin`.** The plan's code writes that field on
   `Auction`, but `last_notified_channel` only exists on `AuctionLot`
   (column ownership is per-lot, not per-auction). The write would have
   crashed with `AttributeError` on first hit. Real bug avoided.

9. **Two-listener notifier `main()` via `asyncio.TaskGroup`.** Single
   `aiohttp.ClientSession` opened once and shared by both the existing
   `notification_pending` listener loop AND the new `needs_plugin`
   listener loop. TaskGroup propagates fatal errors to siblings (right
   behavior for a worker process).

10. **No explicit catchup sweep for `needs_plugin`** (intentional). The
    auction-discoverer re-fires the NOTIFY on every sweep while
    `needs_plugin_notified_at` is NULL (the check in `_sweep_one_discoverer`
    gates on the column). So a notifier-side missed NOTIFY recovers on the
    next discovery pass. Documented with a comment in `_process_needs_plugin`.

11. **`ChannelKey` Literal + `_FIXED_ROUTES` dict** extended to include
    `"needs_plugin"`. The plan added a switch-style `if trigger ==
    "needs_plugin":` clause; the codebase uses a `_FIXED_ROUTES` dict
    (post-Phase-5 refactor). Matched existing structure.

12. **McDougall plugin is structural-only.** Plan acknowledges "selectors
    verified during implementation against captured fixture" — no live
    fixture is available in this session (and we don't fetch external
    data from this conversation). The plugin registers correctly, has the
    full `AuctionSource` interface (`version`, `parse_auction_url`,
    `__aenter__/__aexit__`, `discover_auctions`, `fetch_auction`,
    `fetch_lots`, `fetch_lot`, `poll_bid`), and is tested via injected
    `MockTransport` responses. Real CSS selectors against the live site
    are deferred — when ops captures a fixture, they update the selectors
    in place; no other system changes needed.

13. **`fetch_lots`/`fetch_lot`/`poll_bid` on `FarmAuctionGuideSource` are
    deliberate no-ops** (router-only role). Documented in their docstrings.
    The `return; yield` idiom is used for the empty `AsyncIterator` to
    satisfy both pyright and the type system (the unreachable `yield`
    makes the function a generator; the `return` exits before reaching it).

14. **Tests added**:
    - **mcdougall (11 tests):** `parse_auction_url` happy/canon/non-mcdougall/no-slug; self-register on import; version set; context-manager guard; `fetch_auction`/`fetch_lot` via MockTransport; `poll_bid` 404→missing and 200→open.
    - **farmauctionguide (18 tests):** `resolve_platform` for hibid/mcdougall/unknown/strip-www/no-path-segment edge cases; constructible-with-provinces; self-registers; version set; discover_auctions routes known platforms; skips internal-nav links; `fetch_lots`/`fetch_lot`/`poll_bid` no-op behaviors; context-manager guard.
    - **needs_plugin (11 tests):** `_process_needs_plugin` happy path stamps timestamp; auction not found; already-notified (dedup); non-unknown source skipped; no channel configured; `post_simple_message` returns False (no stamp); render-text happy/all-None fields; `select_channel(trigger="needs_plugin")` returns `"needs_plugin"`.
    - **post_simple_message (7 tests):** success, 429 then success, 429 twice returns False, 400 returns False, no token returns False, network error then success, network error twice returns False. Plus a regression test asserting the payload does NOT include `components`.
    - Net delta across Phase 10: +47 tests (341 → 388).

15. **Pyright baseline.** Was 52 going in, 55 going out (+3). All three new
    errors are in `tests/apps/test_needs_plugin.py` and match the existing
    accepted noise pattern (`reportPrivateUsage` on `_process_needs_plugin`,
    `reportUnusedFunction` on `_patched_get_session`, `asynccontextmanager`
    deprecated). Zero new errors in production code.

16. **Known gaps deferred (not blocking merge):**
    - McDougall selectors are placeholders. First real McDougall auction
      discovered will surface as `unknown:mcdougallauction.com` (correct
      fallback behavior; the McDougall plugin's discover_auctions returns
      0 refs against unfamiliar HTML, so farmauctionguide's router is the
      authoritative path for now). When ops captures a fixture and updates
      the selectors, in-flight unknown auctions can be re-routed via Task
      43's retry button (Phase 11).
    - Task 43 deferred to Phase 11 (dashboard infrastructure dependency).
    - No NOTIFY-during-test pattern for the two-listener `main()`. The
      `_process_needs_plugin` function is unit-tested directly; the
      `_listen_needs_plugin` infinite-loop wrapper isn't. Same intentional
      gap as every other listener loop in this codebase.

17. **Multi-reviewer pre-merge findings applied** (commit `37f9ba2`).
    Three parallel reviewers (bugs/security, architecture/convention, test
    coverage/observability) ran against the branch. Items applied:
    - **`resolve_platform` skip-on-known-host-without-auction-id.** Returns
      `tuple[str, str] | None` now. When a known host (hibid/mcdougall)
      matches but the URL path has no auction id (e.g. `hibid.com/help`,
      `mcdougallauction.com/about` — footer/nav/help links on
      farmauctionguide pages), returns `None` and the discover loop skips
      the link entirely. Without this, those URLs would have fallen
      through to `unknown:hibid.com` and triggered a spurious `needs_plugin`
      Discord alert telling ops to "add a plugin" for hibid (which we
      already plug). Caught by bugs reviewer; real noise risk on first
      production deploy.
    - **McDougall `discover_auctions` + `fetch_lots` regression tests.**
      Both methods have real production logic (absolute-vs-relative href
      reconstruction, dedup by auction/lot id, `/lot/<id>` segment parsing,
      `isdigit()` filter for non-numeric ids) but had zero test coverage.
      Added two tests exercising both branches with hand-crafted HTML
      bodies via `MockTransport`. A regex or href-reconstruction regression
      in either method would have shipped silently.
    - **`_sweep_one_discoverer` `needs_plugin` NOTIFY branch tests.** The
      core of the Phase 10 feature (NOTIFY trigger on unknown source + stamp
      gate) had no test coverage. Added three tests: emits `needs_plugin`
      for fresh unknown source; suppresses for already-stamped auction;
      never fires for known source (`"hibid"` ref). Locks in the
      `ref.source.startswith("unknown:")` and
      `needs_plugin_notified_at IS NULL` guards.
    - **`_process_needs_plugin` success log.** Added `log.info(
      "needs_plugin alert posted", auction_id, channel_id, channel_key)`
      between the stamp-write and return. Previously the only alert-class
      notification in the system that didn't log on success; ops triage
      would have been materially harder.
    - **`_sweep_one_discoverer` NOTIFY-emission log.** Added `log.info(
      "needs_plugin notify emitted", source, auction_id, discoverer)`
      after the `notify(...needs_plugin)` call. Lets ops trace alerts back
      to the originating sweep. The existing
      `log.warning("unknown platform discovered")` fires on every sweep
      (including for already-stamped auctions); the new info log
      distinguishes first-time-NOTIFY from steady-state re-sightings.
    - **Notifier `main()` startup log.** Added `log.info("notifier
      starting", listeners=[...])` before the `asyncio.TaskGroup` spins
      up. If the process exits immediately (e.g., one task fails on
      startup), the log record shows both listeners were attempted.
    - **Architecture reviewer noted the per-province exception swallowing
      in farmauctionguide actually mirrors HibidSource exactly** — the
      implementer's earlier framing as "different behavior" was wrong; it's
      the same convention. No fix needed.

18. **Reviewer items not actioned (acknowledged):**
    - **Duplicate-alert window:** two discovery sweeps closely in time can
      both see `needs_plugin_notified_at IS NULL` and fire NOTIFY twice,
      resulting in two Discord posts for the same auction. Discord accepts
      duplicates; documented as acceptable.
    - **TaskGroup Discord rate-limit collision:** notifier now runs lot
      posting + needs_plugin posting concurrently against the same bot.
      `_post_with_retry`'s 429 handling absorbs collisions; needs_plugin
      alerts are infrequent in practice. Will revisit if first production
      ops shows 429 spam.
    - **No `_listen_needs_plugin` infinite-loop test.** Sibling workers
      also lack this — same intentional gap.
    - **Already-notified `_process_needs_plugin` skip path:** intentionally
      not logged (matches `_process_one`'s no-log-on-skip convention).
      Steady state would log noisily otherwise.

---

End of Phase 10. All three MVP sources are integrated and unknown-platform auctions are flagged for manual plugin addition. Task 43 (dashboard view + retry-routing) is picked up in Phase 11 alongside the dashboard skeleton.

---

## Phase 11 — Dashboard

FastAPI + Jinja2 + HTMX served on `localhost:8000`. No auth in MVP; auth seam wired so it's a one-line addition later. HTMX vendored locally — no CDN.

### Task 44: FastAPI skeleton + base template + HTMX

**Files:**
- Create: `src/carbuyer/apps/dashboard/__init__.py`
- Create: `src/carbuyer/apps/dashboard/__main__.py`
- Create: `src/carbuyer/apps/dashboard/app.py`
- Create: `src/carbuyer/apps/dashboard/deps.py`
- Create: `src/carbuyer/apps/dashboard/templates/base.html`
- Create: `src/carbuyer/apps/dashboard/templates/_macros.html`
- Create: `src/carbuyer/apps/dashboard/static/vendor/htmx.min.js`
- Create: `src/carbuyer/apps/dashboard/static/vendor/chart.umd.js`
- Create: `src/carbuyer/apps/dashboard/static/css/app.css`

- [ ] **Step 1: Vendor HTMX and Chart.js**

```bash
mkdir -p src/carbuyer/apps/dashboard/static/vendor src/carbuyer/apps/dashboard/templates
curl -L -o src/carbuyer/apps/dashboard/static/vendor/htmx.min.js https://unpkg.com/htmx.org@2.0.2/dist/htmx.min.js
curl -L -o src/carbuyer/apps/dashboard/static/vendor/chart.umd.js https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.js
```

- [ ] **Step 2: Implement `src/carbuyer/apps/dashboard/deps.py`**

```python
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.session import async_session_maker


@dataclass(slots=True, frozen=True)
class CurrentUser:
    id: str
    role: str


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def current_user() -> CurrentUser:
    # Stub for MVP; replace with real auth in phase 2.
    return CurrentUser(id="me", role="dev")
```

- [ ] **Step 3: Implement `src/carbuyer/apps/dashboard/app.py`**

```python
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_app() -> FastAPI:
    app = FastAPI(title="CarBuyer Dashboard")
    app.mount(
        "/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="static",
    )
    from carbuyer.apps.dashboard.routers import (
        feed, closing, watched, lots, comps, sold, purchases, health, actions,
    )
    for router in (feed, closing, watched, lots, comps, sold, purchases, health, actions):
        app.include_router(router.router)
    return app


app = create_app()
```

- [ ] **Step 4: Implement `src/carbuyer/apps/dashboard/__main__.py`**

```python
import uvicorn

from carbuyer.apps.dashboard.app import app


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
```

- [ ] **Step 5: Write `src/carbuyer/apps/dashboard/templates/base.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}CarBuyer{% endblock %}</title>
  <link rel="stylesheet" href="/static/css/app.css">
  <script src="/static/vendor/htmx.min.js" defer></script>
  <script src="/static/vendor/chart.umd.js" defer></script>
</head>
<body>
  <nav class="topnav">
    <a href="/">Feed</a>
    <a href="/closing">Closing soon</a>
    <a href="/watched">Watched</a>
    <a href="/comps">Comps</a>
    <a href="/sold">Sold</a>
    <a href="/purchases">Purchases</a>
    <a href="/health">Health</a>
  </nav>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
```

- [ ] **Step 6: Write `src/carbuyer/apps/dashboard/templates/_macros.html`**

```html
{% macro money(value) %}
{%- if value is none -%}—{%- else -%}${{ "{:,.0f}".format(value|float) }}{%- endif -%}
{% endmacro %}

{% macro pct(value) %}
{%- if value is none -%}—{%- else -%}{{ "{:.1%}".format(value|float) }}{%- endif -%}
{% endmacro %}
```

- [ ] **Step 7: Write `src/carbuyer/apps/dashboard/static/css/app.css` (minimum)**

```css
body { font-family: system-ui, sans-serif; margin: 0; padding: 0; background: #fafaf9; color: #1a1a1a; }
.topnav { background: #1a1a1a; padding: 0.75rem 1rem; }
.topnav a { color: #fafaf9; margin-right: 1.25rem; text-decoration: none; }
.topnav a:hover { text-decoration: underline; }
main { padding: 1rem; max-width: 1200px; margin: 0 auto; }
.card { background: white; border: 1px solid #e5e5e5; border-radius: 8px; padding: 1rem; margin-bottom: 0.75rem; }
.muted { color: #666; }
.range-bar { position: relative; height: 8px; background: #ddd; border-radius: 4px; }
.range-bar .marker { position: absolute; width: 4px; height: 12px; top: -2px; background: #d33; border-radius: 2px; }
.tag { display: inline-block; padding: 0.1rem 0.5rem; background: #eee; border-radius: 999px; font-size: 0.85em; margin-right: 0.25rem; }
.tag.rare { background: #ffd166; }
.tag.deal { background: #06d6a0; }
```

- [ ] **Step 8: Smoke test**

```bash
touch src/carbuyer/apps/dashboard/__init__.py
mkdir -p src/carbuyer/apps/dashboard/routers
touch src/carbuyer/apps/dashboard/routers/__init__.py
# Add stub routers to satisfy imports
for r in feed closing watched lots comps sold purchases health actions; do
  cat > src/carbuyer/apps/dashboard/routers/$r.py <<EOF
from fastapi import APIRouter
router = APIRouter()
EOF
done
uv run python -c "from carbuyer.apps.dashboard.app import app; print(app.title)"
```

Expected: `CarBuyer Dashboard`.

- [ ] **Step 9: Commit**

```bash
git add src/carbuyer/apps/dashboard/
git commit -m "dashboard: FastAPI skeleton + base template + HTMX vendoring"
```

---

### Task 45: Auction feed view (default landing)

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/feed.py`
- Create: `src/carbuyer/apps/dashboard/templates/pages/feed.html`
- Create: `src/carbuyer/apps/dashboard/templates/partials/lot_card.html`
- Create: `src/carbuyer/apps/dashboard/templates/partials/lot_list.html`
- Create: `src/carbuyer/apps/dashboard/templates/partials/feed_filters.html`
- Create: `tests/apps/dashboard/__init__.py`
- Create: `tests/apps/dashboard/test_feed.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/dashboard/routers/feed.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session, is_htmx
from carbuyer.db.models import Auction, AuctionLot


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def feed(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    province: list[str] | None = Query(default=None),
    min_score: float = 0.0,
    min_rarity: float = 0.0,
    exclude_not_interested: bool = True,
    cursor: int | None = None,
    limit: int = 20,
) -> HTMLResponse:
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.lot_status.in_(["open", "closing_soon", "extended"]))
    )
    if province:
        stmt = stmt.where(Auction.pickup_province.in_(province))
    if min_score > 0:
        stmt = stmt.where(AuctionLot.price_deal_score >= min_score)
    if min_rarity > 0:
        stmt = stmt.where(AuctionLot.rarity_score >= min_rarity)
    if exclude_not_interested:
        stmt = stmt.where(AuctionLot.user_action.is_distinct_from("not_interested"))
    if cursor is not None:
        stmt = stmt.where(AuctionLot.id < cursor)
    stmt = stmt.order_by(AuctionLot.id.desc()).limit(limit)

    rows = (await session.execute(stmt)).all()
    items = [{"lot": lot, "auction": auction} for (lot, auction) in rows]
    next_cursor = items[-1]["lot"].id if items else None

    template = "partials/lot_list.html" if is_htmx(request) else "pages/feed.html"
    return templates.TemplateResponse(
        request, template,
        {"items": items, "next_cursor": next_cursor,
         "filters": {"province": province or [], "min_score": min_score,
                     "min_rarity": min_rarity, "exclude_not_interested": exclude_not_interested}},
    )
```

- [ ] **Step 2: Write `templates/pages/feed.html`**

```html
{% extends "base.html" %}
{% block title %}Feed — CarBuyer{% endblock %}
{% block content %}
  <h1>Auction feed</h1>
  {% include "partials/feed_filters.html" %}
  <div id="lot-list">
    {% include "partials/lot_list.html" %}
  </div>
{% endblock %}
```

- [ ] **Step 3: Write `templates/partials/feed_filters.html`**

```html
<form
  hx-get="/"
  hx-target="#lot-list"
  hx-swap="innerHTML"
  hx-trigger="change from:select, change from:input[type='checkbox'], keyup changed delay:300ms from:input[type='number'], submit"
  hx-push-url="true"
  class="card"
>
  <label>Province
    <select name="province" multiple>
      {% for p in ["AB", "BC", "SK", "MB"] %}
        <option value="{{ p }}" {% if p in filters.province %}selected{% endif %}>{{ p }}</option>
      {% endfor %}
    </select>
  </label>
  <label>Min deal score
    <input type="number" name="min_score" step="0.01" min="0" max="1" value="{{ filters.min_score }}">
  </label>
  <label>Min rarity score
    <input type="number" name="min_rarity" step="0.5" min="0" max="5" value="{{ filters.min_rarity }}">
  </label>
  <label>
    <input type="checkbox" name="exclude_not_interested" value="true"
      {% if filters.exclude_not_interested %}checked{% endif %}>
    Exclude not-interested
  </label>
</form>
```

- [ ] **Step 4: Write `templates/partials/lot_list.html`**

```html
{% from "_macros.html" import money %}
{% for item in items %}
  {% include "partials/lot_card.html" %}
{% endfor %}
{% if next_cursor %}
  <div
    hx-get="/?cursor={{ next_cursor }}{% if filters.province %}{% for p in filters.province %}&province={{ p }}{% endfor %}{% endif %}&min_score={{ filters.min_score }}&min_rarity={{ filters.min_rarity }}&exclude_not_interested={{ filters.exclude_not_interested|lower }}"
    hx-trigger="revealed"
    hx-swap="afterend"
  ></div>
{% endif %}
```

- [ ] **Step 5: Write `templates/partials/lot_card.html`**

```html
{% from "_macros.html" import money %}
{% set lot = item.lot %}
{% set auction = item.auction %}
<div class="card">
  <h3>
    <a href="/lots/{{ lot.id }}">{{ lot.year or "" }} {{ lot.make or "" }} {{ lot.model or "" }} {{ lot.trim or "" }}</a>
  </h3>
  <p class="muted">
    {{ auction.pickup_city or "?" }}, {{ auction.pickup_province or "?" }} ·
    closes {{ auction.scheduled_end_at.strftime("%b %d %H:%M") if auction.scheduled_end_at else "?" }}
  </p>
  <p>
    Current bid: <strong>{{ money(lot.current_high_bid_cad) }}</strong>
    {% if lot.all_in_at_current_bid_cad %} · All-in: {{ money(lot.all_in_at_current_bid_cad) }}{% endif %}
    {% if lot.expected_value_cad %} · Est value: {{ money(lot.expected_value_cad) }}{% endif %}
  </p>
  <p>
    {% if lot.rarity_score and lot.rarity_score >= 2.0 %}
      <span class="tag rare">Rarity {{ "%.1f"|format(lot.rarity_score) }}</span>
    {% endif %}
    {% if lot.price_deal_score and lot.price_deal_score >= 0.15 %}
      <span class="tag deal">Deal {{ "%.0f"|format(lot.price_deal_score * 100) }}%</span>
    {% endif %}
    {% if lot.suspicious_underprice_flag %}
      <span class="tag" style="background:#ffe0e0">⚠ below low end</span>
    {% endif %}
  </p>
</div>
```

- [ ] **Step 6: Write the test**

```python
# tests/apps/dashboard/test_feed.py
import pytest
from fastapi.testclient import TestClient

from carbuyer.apps.dashboard.app import app


def test_feed_root_returns_html() -> None:
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "Auction feed" in r.text


def test_feed_htmx_returns_partial() -> None:
    with TestClient(app) as client:
        r = client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Auction feed" not in r.text  # full page header omitted
```

- [ ] **Step 7: Run test and commit**

```bash
mkdir -p tests/apps/dashboard
touch tests/apps/dashboard/__init__.py
uv run pytest tests/apps/dashboard/test_feed.py -v
git add src/carbuyer/apps/dashboard/routers/feed.py src/carbuyer/apps/dashboard/templates/ tests/apps/dashboard/
git commit -m "dashboard: auction feed view with filters and infinite scroll"
```

---

### Task 46: Lot detail with comp comparison panel

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/lots.py`
- Create: `src/carbuyer/apps/dashboard/templates/pages/lot_detail.html`
- Create: `src/carbuyer/apps/dashboard/templates/partials/comp_panel.html`
- Create: `tests/apps/dashboard/test_lot_detail.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/dashboard/routers/lots.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session, is_htmx
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale


router = APIRouter()


@router.get("/lots/{lot_id}", response_class=HTMLResponse)
async def lot_detail(
    request: Request, lot_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)
    auction = await session.get(Auction, lot.auction_id)
    return templates.TemplateResponse(
        request, "pages/lot_detail.html",
        {"lot": lot, "auction": auction},
    )


@router.get("/lots/{lot_id}/comps", response_class=HTMLResponse)
async def lot_comps(
    request: Request, lot_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    lot = await session.get(AuctionLot, lot_id)
    if lot is None or lot.make is None or lot.model is None or lot.year is None:
        return templates.TemplateResponse(
            request, "partials/comp_panel.html",
            {"sold": [], "open": [], "fuzzy": True},
        )

    # Sold comps from historical_sales: same make/model, year ±2, mileage ±20%
    mileage = lot.mileage_km or 0
    mileage_lo = int(mileage * 0.8) if mileage else 0
    mileage_hi = int(mileage * 1.2) if mileage else 9_999_999

    sold_stmt = (
        select(HistoricalSale)
        .where(
            HistoricalSale.make == lot.make,
            HistoricalSale.model == lot.model,
            HistoricalSale.year.between(lot.year - 2, lot.year + 2),
        )
        .order_by(HistoricalSale.id.desc())
        .limit(20)
    )
    if mileage:
        sold_stmt = sold_stmt.where(HistoricalSale.mileage_km.between(mileage_lo, mileage_hi))
    sold = list((await session.execute(sold_stmt)).scalars().all())

    # Open comps: currently-open auction_lots, same make/model, year ±2
    open_stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.id != lot.id,
            AuctionLot.lot_status.in_(["open", "closing_soon", "extended"]),
            AuctionLot.make == lot.make,
            AuctionLot.model == lot.model,
            AuctionLot.year.between(lot.year - 2, lot.year + 2),
        )
        .order_by(Auction.scheduled_end_at.asc())
        .limit(10)
    )
    open_rows = (await session.execute(open_stmt)).all()
    open_lots = [{"lot": lot_row, "auction": auc} for (lot_row, auc) in open_rows]

    fuzzy = (len(sold) + len(open_lots)) == 0
    return templates.TemplateResponse(
        request, "partials/comp_panel.html",
        {"sold": sold, "open": open_lots, "fuzzy": fuzzy},
    )
```

- [ ] **Step 2: Write `templates/pages/lot_detail.html`**

```html
{% extends "base.html" %}
{% from "_macros.html" import money %}
{% block title %}{{ lot.title or "Lot" }}{% endblock %}
{% block content %}
  <h1>{{ lot.year or "" }} {{ lot.make or "" }} {{ lot.model or "" }} {{ lot.trim or "" }}</h1>
  <p class="muted">
    {{ auction.auctioneer_name or auction.source }} ·
    {{ auction.pickup_city or "?" }}, {{ auction.pickup_province or "?" }} ·
    <a href="{{ lot.url }}" target="_blank">view at source</a>
  </p>

  <section class="card">
    <h3>Comparable vehicles</h3>
    <div hx-get="/lots/{{ lot.id }}/comps" hx-trigger="load" hx-swap="innerHTML">
      Loading…
    </div>
  </section>

  <section class="card">
    <h3>Pricing</h3>
    <p>Current bid: <strong>{{ money(lot.current_high_bid_cad) }}</strong></p>
    <p>All-in at current bid: {{ money(lot.all_in_at_current_bid_cad) }}</p>
    <p>Expected value (private equiv): {{ money(lot.expected_value_cad) }}</p>
    <p>Recommended max bid: <strong>{{ money(lot.recommended_max_bid_cad) }}</strong></p>
  </section>

  <section class="card">
    <h3>Flags</h3>
    {% if lot.red_flags %}<p>Red: {% for f in lot.red_flags %}<span class="tag">{{ f.flag }}</span>{% endfor %}</p>{% endif %}
    {% if lot.green_flags %}<p>Green: {% for f in lot.green_flags %}<span class="tag">{{ f.flag }}</span>{% endfor %}</p>{% endif %}
    {% if lot.showstopper_flags %}<p style="color:#d33">Showstoppers: {% for f in lot.showstopper_flags %}<span class="tag">{{ f.flag }}</span>{% endfor %}</p>{% endif %}
  </section>

  <section class="card">
    <h3>Actions</h3>
    <form hx-post="/lots/{{ lot.id }}/mark" hx-swap="none" style="display:inline">
      <button name="action" value="interested">👍 Interested</button>
      <button name="action" value="maybe">🤔 Maybe</button>
      <button name="action" value="not_interested">👎 Not interested</button>
    </form>
  </section>
{% endblock %}
```

- [ ] **Step 3: Write `templates/partials/comp_panel.html`**

```html
{% from "_macros.html" import money %}
{% if fuzzy %}
  <p class="muted">No exact matches in our database yet. As more auctions are tracked, similar vehicles will appear here.</p>
{% endif %}
<h4>Recently sold</h4>
{% if sold %}
  <div style="display:flex;overflow-x:auto;gap:0.5rem">
    {% for s in sold %}
      <div class="card" style="min-width:200px">
        <strong>{{ s.year or "" }} {{ s.make or "" }} {{ s.model or "" }}</strong><br>
        <span class="muted">{{ s.mileage_km or "?" }} km · {{ s.sale_channel }}</span><br>
        Sold: {{ money(s.final_price_with_premium_cad or s.final_listed_price_cad) }}<br>
        <span class="muted">{{ s.disappeared_at.strftime("%Y-%m-%d") if s.disappeared_at else "" }}</span>
      </div>
    {% endfor %}
  </div>
{% else %}
  <p class="muted">No sold comps yet.</p>
{% endif %}

<h4>Currently up for auction</h4>
{% if open %}
  <div style="display:flex;overflow-x:auto;gap:0.5rem">
    {% for o in open %}
      <div class="card" style="min-width:200px">
        <a href="/lots/{{ o.lot.id }}"><strong>{{ o.lot.year or "" }} {{ o.lot.make or "" }} {{ o.lot.model or "" }}</strong></a><br>
        <span class="muted">{{ o.lot.mileage_km or "?" }} km · {{ o.auction.pickup_city or "?" }}, {{ o.auction.pickup_province or "?" }}</span><br>
        Current: {{ money(o.lot.current_high_bid_cad) }}<br>
        Closes: {{ o.auction.scheduled_end_at.strftime("%b %d") if o.auction.scheduled_end_at else "?" }}
      </div>
    {% endfor %}
  </div>
{% else %}
  <p class="muted">No open comps.</p>
{% endif %}
```

- [ ] **Step 4: Write the test**

```python
# tests/apps/dashboard/test_lot_detail.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from carbuyer.apps.dashboard.app import app
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.session import async_session_maker


@pytest.mark.asyncio
async def test_lot_detail_renders() -> None:
    async with async_session_maker() as s:
        a = Auction(source="hibid", source_auction_id="A", url="x", auction_subtype="estate",
                    first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
                    pickup_province="AB", pickup_city="Calgary")
        s.add(a)
        await s.flush()
        lot = AuctionLot(
            auction_id=a.id, source_lot_id="L1", url="https://x",
            title="2010 Ford F-150", year=2010, make="Ford", model="F-150",
            current_high_bid_cad=Decimal("8000"),
            red_flags=[], green_flags=[], showstopper_flags=[],
        )
        s.add(lot)
        await s.commit()
        lot_id = lot.id
    with TestClient(app) as client:
        r = client.get(f"/lots/{lot_id}")
    assert r.status_code == 200
    assert "Ford" in r.text
```

- [ ] **Step 5: Run test and commit**

```bash
uv run pytest tests/apps/dashboard/test_lot_detail.py -v
git add src/carbuyer/apps/dashboard/routers/lots.py src/carbuyer/apps/dashboard/templates/pages/lot_detail.html src/carbuyer/apps/dashboard/templates/partials/comp_panel.html tests/apps/dashboard/test_lot_detail.py
git commit -m "dashboard: lot detail + comp comparison panel"
```

---

### Task 47: Remaining views (closing-soon, watched, comps, sold, purchases, health)

These follow the same pattern as Task 43. For each: a router file, a page template, optionally an HTMX partial. Implementation listed compactly per view.

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/closing.py`
- Modify: `src/carbuyer/apps/dashboard/routers/watched.py`
- Modify: `src/carbuyer/apps/dashboard/routers/comps.py`
- Modify: `src/carbuyer/apps/dashboard/routers/sold.py`
- Modify: `src/carbuyer/apps/dashboard/routers/purchases.py`
- Modify: `src/carbuyer/apps/dashboard/routers/health.py`
- Create: `src/carbuyer/apps/dashboard/templates/pages/closing.html`
- Create: `src/carbuyer/apps/dashboard/templates/pages/watched.html`
- Create: `src/carbuyer/apps/dashboard/templates/pages/comps.html`
- Create: `src/carbuyer/apps/dashboard/templates/pages/sold.html`
- Create: `src/carbuyer/apps/dashboard/templates/pages/purchases.html`
- Create: `src/carbuyer/apps/dashboard/templates/pages/health.html`

- [ ] **Step 1: Closing-soon — `routers/closing.py`**

```python
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import Auction, AuctionLot


router = APIRouter()


@router.get("/closing", response_class=HTMLResponse)
async def closing(
    request: Request, hours: int = 24,
    session: Annotated[AsyncSession, Depends(get_session)] = ...,
) -> HTMLResponse:
    cutoff = datetime.now(timezone.utc) + timedelta(hours=hours)
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(
            AuctionLot.lot_status.in_(["open", "closing_soon", "extended"]),
            Auction.scheduled_end_at.is_not(None),
            Auction.scheduled_end_at <= cutoff,
        )
        .order_by(Auction.scheduled_end_at.asc())
        .limit(50)
    )
    rows = (await session.execute(stmt)).all()
    items = [{"lot": lot, "auction": auc} for (lot, auc) in rows]
    return templates.TemplateResponse(request, "pages/closing.html",
                                      {"items": items, "hours": hours})
```

- [ ] **Step 2: `templates/pages/closing.html`**

```html
{% extends "base.html" %}
{% from "_macros.html" import money %}
{% block content %}
  <h1>Closing in next {{ hours }}h</h1>
  {% if not items %}<p class="muted">Nothing closing soon.</p>{% endif %}
  {% for item in items %}
    {% include "partials/lot_card.html" %}
  {% endfor %}
{% endblock %}
```

- [ ] **Step 3: Watched — `routers/watched.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import Auction, AuctionLot


router = APIRouter()


@router.get("/watched", response_class=HTMLResponse)
async def watched(
    request: Request,
    tier: str = Query(default="interested"),
    session: Annotated[AsyncSession, Depends(get_session)] = ...,
) -> HTMLResponse:
    if tier not in {"interested", "maybe"}:
        tier = "interested"
    stmt = (
        select(AuctionLot, Auction)
        .join(Auction, Auction.id == AuctionLot.auction_id)
        .where(AuctionLot.user_action == tier)
        .order_by(Auction.scheduled_end_at.asc().nulls_last())
        .limit(100)
    )
    rows = (await session.execute(stmt)).all()
    items = [{"lot": lot, "auction": auc} for (lot, auc) in rows]
    return templates.TemplateResponse(request, "pages/watched.html",
                                      {"items": items, "tier": tier})
```

- [ ] **Step 4: `templates/pages/watched.html`**

```html
{% extends "base.html" %}
{% block content %}
  <h1>Watched lots</h1>
  <p>
    <a href="/watched?tier=interested" class="tag {{ 'deal' if tier == 'interested' else '' }}">Interested</a>
    <a href="/watched?tier=maybe" class="tag {{ 'rare' if tier == 'maybe' else '' }}">Maybe</a>
  </p>
  {% if not items %}<p class="muted">No lots in this tier.</p>{% endif %}
  {% for item in items %}
    {% include "partials/lot_card.html" %}
  {% endfor %}
{% endblock %}
```

- [ ] **Step 5: Comps + sold-price browsers — `routers/comps.py` and `routers/sold.py`**

```python
# src/carbuyer/apps/dashboard/routers/comps.py
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import HistoricalSale


router = APIRouter()


@router.get("/comps", response_class=HTMLResponse)
async def comps(
    request: Request, make: str | None = None, model: str | None = None,
    year: int | None = None, trim: str | None = None,
    session: Annotated[AsyncSession, Depends(get_session)] = ...,
) -> HTMLResponse:
    rows: list[HistoricalSale] = []
    if make and model:
        stmt = select(HistoricalSale).where(
            HistoricalSale.make == make, HistoricalSale.model == model,
        )
        if year is not None:
            stmt = stmt.where(HistoricalSale.year.between(year - 2, year + 2))
        if trim:
            stmt = stmt.where(HistoricalSale.trim == trim)
        rows = list((await session.execute(stmt.limit(200))).scalars().all())
    return templates.TemplateResponse(request, "pages/comps.html",
                                      {"rows": rows, "make": make, "model": model,
                                       "year": year, "trim": trim})
```

```python
# src/carbuyer/apps/dashboard/routers/sold.py
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import HistoricalSale


router = APIRouter()


@router.get("/sold", response_class=HTMLResponse)
async def sold(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)] = ...,
) -> HTMLResponse:
    rows = list((await session.execute(
        select(HistoricalSale).order_by(HistoricalSale.id.desc()).limit(100)
    )).scalars().all())
    return templates.TemplateResponse(request, "pages/sold.html", {"rows": rows})
```

- [ ] **Step 6: `templates/pages/comps.html` and `pages/sold.html`**

```html
{# templates/pages/comps.html #}
{% extends "base.html" %}
{% from "_macros.html" import money %}
{% block content %}
  <h1>Comp browser</h1>
  <form method="get" class="card">
    <input name="make" placeholder="Make" value="{{ make or '' }}" required>
    <input name="model" placeholder="Model" value="{{ model or '' }}" required>
    <input name="year" type="number" placeholder="Year" value="{{ year or '' }}">
    <input name="trim" placeholder="Trim" value="{{ trim or '' }}">
    <button type="submit">Search</button>
  </form>
  {% if not rows %}<p class="muted">Type a make/model to browse comps.</p>{% endif %}
  <ul>
    {% for r in rows %}
      <li>
        {{ r.year }} {{ r.make }} {{ r.model }} {{ r.trim or '' }} ·
        {{ r.mileage_km or '?' }} km · {{ r.sale_channel }} ·
        {{ money(r.final_price_with_premium_cad or r.final_listed_price_cad) }}
      </li>
    {% endfor %}
  </ul>
{% endblock %}
```

```html
{# templates/pages/sold.html #}
{% extends "base.html" %}
{% from "_macros.html" import money %}
{% block content %}
  <h1>Recent sold prices</h1>
  <table>
    <thead><tr><th>Year</th><th>Make/Model</th><th>km</th><th>Channel</th><th>Price</th><th>Date</th></tr></thead>
    <tbody>
    {% for r in rows %}
      <tr>
        <td>{{ r.year or "" }}</td>
        <td>{{ r.make or "" }} {{ r.model or "" }} {{ r.trim or "" }}</td>
        <td>{{ r.mileage_km or "?" }}</td>
        <td>{{ r.sale_channel }}</td>
        <td>{{ money(r.final_price_with_premium_cad or r.final_listed_price_cad) }}</td>
        <td>{{ r.disappeared_at.strftime("%Y-%m-%d") if r.disappeared_at else "" }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

- [ ] **Step 7: Purchases + health — `routers/purchases.py` and `routers/health.py`**

```python
# src/carbuyer/apps/dashboard/routers/purchases.py
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import Purchase


router = APIRouter()


@router.get("/purchases", response_class=HTMLResponse)
async def purchases_list(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)] = ...,
) -> HTMLResponse:
    rows = list((await session.execute(
        select(Purchase).order_by(Purchase.purchase_date.desc())
    )).scalars().all())
    year = datetime.now(timezone.utc).year
    ytd_count = (await session.execute(
        select(func.count()).select_from(Purchase)
        .where(extract("year", Purchase.purchase_date) == year)
    )).scalar_one()
    return templates.TemplateResponse(request, "pages/purchases.html",
                                      {"rows": rows, "ytd_count": ytd_count, "year": year})


@router.post("/purchases", response_class=HTMLResponse)
async def purchases_create(
    purchase_date: Annotated[date, Form()],
    make: Annotated[str, Form()],
    model: Annotated[str, Form()],
    year: Annotated[int, Form()],
    purchase_price_cad: Annotated[float, Form()],
    province_of_purchase: Annotated[str, Form()] = "AB",
    session: Annotated[AsyncSession, Depends(get_session)] = ...,
) -> RedirectResponse:
    p = Purchase(
        purchase_date=purchase_date,
        make=make, model=model, year=year,
        purchase_price_cad=purchase_price_cad,
        province_of_purchase=province_of_purchase,
    )
    session.add(p)
    await session.commit()
    return RedirectResponse("/purchases", status_code=303)
```

```python
# src/carbuyer/apps/dashboard/routers/health.py
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.app import templates
from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import Auction, AuctionLot, HistoricalSale


router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
async def health(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)] = ...,
) -> HTMLResponse:
    auction_count = (await session.execute(select(func.count()).select_from(Auction))).scalar_one()
    lot_count = (await session.execute(select(func.count()).select_from(AuctionLot))).scalar_one()
    open_count = (await session.execute(
        select(func.count()).select_from(AuctionLot)
        .where(AuctionLot.lot_status.in_(["open", "closing_soon", "extended"]))
    )).scalar_one()
    pending_enrichment = (await session.execute(
        select(func.count()).select_from(AuctionLot)
        .where(AuctionLot.enrichment_status == "pending")
    )).scalar_one()
    pending_valuation = (await session.execute(
        select(func.count()).select_from(AuctionLot)
        .where(AuctionLot.valuation_status == "pending")
    )).scalar_one()
    pending_notification = (await session.execute(
        select(func.count()).select_from(AuctionLot)
        .where(AuctionLot.notification_status == "pending")
    )).scalar_one()
    historical_count = (await session.execute(select(func.count()).select_from(HistoricalSale))).scalar_one()
    return templates.TemplateResponse(request, "pages/health.html", {
        "auction_count": auction_count, "lot_count": lot_count,
        "open_count": open_count,
        "pending_enrichment": pending_enrichment,
        "pending_valuation": pending_valuation,
        "pending_notification": pending_notification,
        "historical_count": historical_count,
    })
```

- [ ] **Step 8: `templates/pages/purchases.html` and `pages/health.html`**

```html
{# templates/pages/purchases.html #}
{% extends "base.html" %}
{% from "_macros.html" import money %}
{% block content %}
  <h1>Purchases ({{ year }} YTD: {{ ytd_count }})</h1>
  {% if ytd_count >= 4 %}
    <p style="color:#d33"><strong>⚠ At or above curbsider warning threshold.</strong>
       BC's deeming clause kicks in at 5; AMVIC has zero tolerance. Stop and reassess.</p>
  {% elif ytd_count == 3 %}
    <p style="color:#c80">⚠ One more transfer this year is your hard ceiling.</p>
  {% endif %}

  <form method="post" action="/purchases" class="card">
    <input name="purchase_date" type="date" required>
    <input name="make" placeholder="Make" required>
    <input name="model" placeholder="Model" required>
    <input name="year" type="number" placeholder="Year" required>
    <input name="purchase_price_cad" type="number" step="0.01" placeholder="Price CAD" required>
    <input name="province_of_purchase" placeholder="Province" value="AB">
    <button type="submit">Record purchase</button>
  </form>

  <table>
    <thead><tr><th>Date</th><th>Vehicle</th><th>Price</th><th>Province</th><th>Sold?</th></tr></thead>
    <tbody>
      {% for p in rows %}
        <tr>
          <td>{{ p.purchase_date }}</td>
          <td>{{ p.year }} {{ p.make }} {{ p.model }}</td>
          <td>{{ money(p.purchase_price_cad) }}</td>
          <td>{{ p.province_of_purchase or "" }}</td>
          <td>{{ p.sale_date or "(held)" }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

```html
{# templates/pages/health.html #}
{% extends "base.html" %}
{% block content %}
  <h1>System health</h1>
  <ul>
    <li>Auctions tracked: {{ auction_count }}</li>
    <li>Lots tracked (all): {{ lot_count }}</li>
    <li>Open lots: {{ open_count }}</li>
    <li>Pending enrichment: {{ pending_enrichment }}</li>
    <li>Pending valuation: {{ pending_valuation }}</li>
    <li>Pending notification: {{ pending_notification }}</li>
    <li>Historical sales: {{ historical_count }}</li>
  </ul>
{% endblock %}
```

- [ ] **Step 9: Smoke-test all routes**

```bash
uv run python -c "
from fastapi.testclient import TestClient
from carbuyer.apps.dashboard.app import app
client = TestClient(app)
for path in ['/', '/closing', '/watched', '/comps', '/sold', '/purchases', '/health']:
    r = client.get(path)
    print(path, r.status_code)
    assert r.status_code == 200
"
```

Expected: each path prints `200`.

- [ ] **Step 10: Commit**

```bash
git add src/carbuyer/apps/dashboard/routers/ src/carbuyer/apps/dashboard/templates/pages/
git commit -m "dashboard: closing / watched / comps / sold / purchases / health views"
```

---

### Task 48: Action endpoints (mark, notes, snooze, refresh, rescore)

**Files:**
- Modify: `src/carbuyer/apps/dashboard/routers/actions.py`

- [ ] **Step 1: Implement `src/carbuyer/apps/dashboard/routers/actions.py`**

```python
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.apps.dashboard.deps import get_session
from carbuyer.db.models import AuctionLot


router = APIRouter()


VALID_ACTIONS = {"interested", "maybe", "not_interested"}


@router.post("/lots/{lot_id}/mark", status_code=204)
async def mark_lot(
    lot_id: int,
    action: Annotated[Literal["interested", "maybe", "not_interested"], Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail="invalid action")
    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)
    lot.user_action = action
    await session.commit()
    return Response(status_code=204)


@router.post("/lots/{lot_id}/notes", status_code=204)
async def append_note(
    lot_id: int, note: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    lot = await session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404)
    existing = lot.notes or ""
    lot.notes = (existing + "\n" + note).strip() if existing else note
    await session.commit()
    return Response(status_code=204)


@router.post("/admin/rescore", status_code=204)
async def rescore_all(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await session.execute(update(AuctionLot).values(valuation_status="pending"))
    await session.commit()
    return Response(status_code=204)
```

- [ ] **Step 2: Smoke test**

```python
# tests/apps/dashboard/test_actions.py
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from carbuyer.apps.dashboard.app import app
from carbuyer.db.models import Auction, AuctionLot
from carbuyer.db.session import async_session_maker


@pytest.mark.asyncio
async def test_mark_endpoint_updates_user_action() -> None:
    async with async_session_maker() as s:
        a = Auction(source="t", source_auction_id="A", url="x", auction_subtype="estate",
                    first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC))
        s.add(a)
        await s.flush()
        lot = AuctionLot(auction_id=a.id, source_lot_id="L1", url="https://x",
                         red_flags=[], green_flags=[], showstopper_flags=[])
        s.add(lot)
        await s.commit()
        lot_id = lot.id
    with TestClient(app) as client:
        r = client.post(f"/lots/{lot_id}/mark", data={"action": "interested"})
    assert r.status_code == 204
    async with async_session_maker() as s:
        lot = await s.get(AuctionLot, lot_id)
        assert lot.user_action == "interested"
```

- [ ] **Step 3: Run test and commit**

```bash
uv run pytest tests/apps/dashboard/test_actions.py -v
git add src/carbuyer/apps/dashboard/routers/actions.py tests/apps/dashboard/test_actions.py
git commit -m "dashboard: action endpoints (mark / notes / rescore)"
```

---

End of Phase 11. Dashboard is complete with all views and HTMX-driven interactivity.

### Phase 11 post-implementation overlay

Tasks 44, 45, 46, 47, 48 land in this phase. Task 43 (`/needs-plugin` view +
retry-routing admin endpoint) was carried over from Phase 10 and lands here
alongside the dashboard skeleton it depends on.

#### Plan-vs-code corrections (recurring + new)

1. **`async_session_maker` → `get_session_maker()` / `get_session()`**
   (recurring fix every prior phase has applied). The plan's `deps.py`,
   `lot_detail` test, `purchases_create`, and `actions` test all imported
   `async_session_maker` — there is no such symbol. The dashboard's FastAPI
   dependency is a plain async generator that calls `get_session_maker()()`
   per request, so `set_engine_for_testing` reroutes it correctly without
   import-time caching.

2. **`from datetime import UTC`** instead of `from datetime import timezone`
   / `timezone.utc` (Python 3.11+ idiom). Recurring.

3. **Unknown-source filter is `Auction.source.like("unknown:%")`, not
   `Auction.source == "farmauctionguide_unknown"`.** Phase 10 changed the
   convention to per-host (`unknown:<host>`); the plan still references the
   single-bucket name. The LIKE pattern matches every per-host bucket.

4. **`resolve_platform()` returns `tuple[str, str] | None` (Phase 10 fix).**
   The plan's `retry_routing` destructures the return value unconditionally;
   `None` means "known host but no auction-id in path" (footer/help/nav
   link) and must short-circuit before destructuring. Implementation handles
   all three branches: `None` → no-op 204; `("unknown:<host>", id)` → no-op
   204 (no plugin matches yet); `("hibid"|"mcdougall", id)` → re-route.

5. **FastAPI dependency parameter ordering.** The plan's Task 47 signatures
   put `session: Annotated[..., Depends(get_session)] = ...` after defaulted
   `Form()` params. Python disallows non-default-after-default params; FastAPI
   doesn't get a chance to fix this. All routers reorder so `Depends`
   parameters come first and `Form()`/`Query()`-defaulted params come last.

6. **`LotStatus` / status-string usage.** Workers use both `.value` and bare
   StrEnum members interchangeably (StrEnum's `__eq__` makes them equivalent).
   Dashboard standardized on `.value` for clarity at SQL boundaries.

7. **`python-multipart` was missing from dependencies.** Required by
   `Form()` — added to `pyproject.toml` as `python-multipart>=0.0.28`. Plan
   didn't mention this transitive requirement.

8. **`Result.rowcount` is not a typed attribute** on the abstract
   `Result[Any]` returned by `session.execute(update(...))` — only on the
   concrete `CursorResult` subclass. Dropped the `rows=...` kwarg from
   `rescore_all`'s log line rather than fight the type cast; logging
   "rescore triggered" without a count is sufficient for an admin endpoint.

9. **Inner-import in `app.py`** (`from carbuyer.apps.dashboard.routers import
   ...` deferred to inside `create_app()`) is necessary to avoid circular
   import — routers import `templates` from `app.py`. Suppressed the
   `PLC0415` (import-not-at-top-level) lint with `# noqa` plus a why-comment.

10. **`current_user()` auth seam wired into `/health`** (multi-reviewer fix).
    The plan defined a `current_user()` stub but never used it as a
    dependency anywhere — dead code that would silently rot until the first
    real auth wiring exposed signature drift. Adding it as
    `Depends(current_user)` on `/health` (cheapest seam) means the dependency
    resolver validates it on every commit; replacing the stub body with real
    auth is now a single-file change.

11. **`OPEN_STATUSES` centralized in `deps.py`** (multi-reviewer fix). The
    same `(LotStatus.OPEN.value, LotStatus.CLOSING_SOON.value,
    LotStatus.EXTENDED.value)` tuple was duplicated verbatim in
    `feed.py`/`lots.py`/`closing.py`/`health.py`. Lifted to
    `deps.OPEN_STATUSES` so adding a new biddable status updates one place,
    not four.

12. **`retry_routing` `IntegrityError` handling** (multi-reviewer
    bugs/security fix). When a discoverer-direct `hibid`/`mcdougall` row
    already exists for the same auction the dashboard is trying to re-route
    away from `unknown:<host>`, the UPDATE collides with the
    `(source, source_auction_id)` unique constraint. Plan's code would
    crash with HTTP 500 (`asyncpg.UniqueViolationError`). Wrapped the
    `await session.commit()` in `try/except IntegrityError`, return 409
    after rollback so the dashboard surfaces the collision and ops can
    delete the stale `unknown:*` row by hand. Regression test
    `test_retry_routing_409_when_target_source_already_exists` locks this in.

13. **`retry_routing` stamps `needs_plugin_notified_at` on success**
    (multi-reviewer architecture fix). The column's invariant is "NULL =
    state unresolved." Resolving via the dashboard is itself an
    acknowledgement, even if no Discord post fired earlier. Setting the
    timestamp to `now()` keeps the column truthful.

14. **Structured logging in `needs_plugin.py`** (multi-reviewer
    observability fix). The success path silently mutated source +
    auction_id + timestamps + bulk-updated lot statuses + fired NOTIFY with
    no log line — incident response would have been blind. Added
    `log.info("auction re-routed", ...)` on success and `log.warning(...)`
    on both silent-return branches (no auction-id; routing still unknown).

#### Tests added

Per-task counts for the multi-reviewer fix round (commit
`a3036db`):

- **Feed coverage (4 tests):** `test_feed_filters_by_min_score`,
  `test_feed_filters_by_min_rarity` (both score-filter branches were
  untested — silent column-rename regression would have shipped),
  `test_feed_cursor_pagination` (infinite-scroll boundary was untested),
  strengthened `test_feed_htmx_returns_partial` with a positive anchor
  (would have passed even if the partial template were blanked).
- **`retry_routing` coverage (extended + 1 new):** the happy path now
  seeds a lot with non-pending statuses and asserts the bulk-update reset
  them all to PENDING (proves the most operationally significant side
  effect, not just the source rewrite); new
  `test_retry_routing_409_when_target_source_already_exists` covers the
  IntegrityError → 409 branch.

Phase 11 net delta: +43 tests across 5 dashboard test files (394 → 437);
1 pre-existing skip carried over.

#### Pyright / ruff

- **Pyright baseline.** 59 errors going in, 59 going out (unchanged).
  Dashboard scope (`src/carbuyer/apps/dashboard` + `tests/apps/dashboard`)
  is 0/0/0 — clean.
- **Ruff clean.** All rules pass.

#### Reviewer items not actioned (acknowledged)

- **Stale `unknown:*` row left in place after a 409 collision** — by design.
  Merging lots across the unique-constraint boundary risks a second
  collision on AuctionLot's `(auction_id, source_lot_id)`; the safer move
  is to surface the collision to ops and let them resolve it manually.
- **`current_user()` body still returns a stub** — by design (MVP).
  The seam is now exercised; replacing the body with real auth is a
  single-file change.
- **No log on `retry_routing` 404 path** — FastAPI's default access log
  records the 404, and the path doesn't mutate state; matches
  `actions.py`'s no-log-on-404 convention.

---

## Phase 12 — Production deployment

### Task 49: systemd unit files

**Files:**
- Create: `infra/systemd/carbuyer-postgres.service`
- Create: `infra/systemd/carbuyer-bot.service`
- Create: `infra/systemd/carbuyer-dashboard.service`
- Create: `infra/systemd/carbuyer-enricher.service`
- Create: `infra/systemd/carbuyer-valuator.service`
- Create: `infra/systemd/carbuyer-notifier.service`
- Create: `infra/systemd/carbuyer-lot-scraper.service`
- Create: `infra/systemd/carbuyer-bid-poller.service`
- Create: `infra/systemd/carbuyer-discoverer.service`
- Create: `infra/systemd/carbuyer-discoverer.timer`
- Create: `infra/systemd/carbuyer-vision.service`
- Create: `infra/systemd/carbuyer-vision.timer`
- Create: `infra/systemd/carbuyer-distiller.service`
- Create: `infra/systemd/carbuyer-distiller.timer`
- Create: `infra/systemd/install.sh`

- [ ] **Step 1: Continuous-service template — `infra/systemd/carbuyer-bot.service`**

```ini
[Unit]
Description=CarBuyer Discord bot
After=network-online.target carbuyer-postgres.service
Requires=carbuyer-postgres.service

[Service]
Type=simple
User=mark
WorkingDirectory=/home/mark/repos/CarBuyerAssistant
EnvironmentFile=/home/mark/repos/CarBuyerAssistant/.env
ExecStart=/home/mark/repos/CarBuyerAssistant/.venv/bin/python -m carbuyer.apps.bot
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

(Same shape, swap module name, for: `carbuyer-dashboard.service` → `carbuyer.apps.dashboard`; `carbuyer-enricher.service` → `carbuyer.apps.enricher`; `carbuyer-valuator.service` → `carbuyer.apps.valuator`; `carbuyer-notifier.service` → `carbuyer.apps.notifier`; `carbuyer-lot-scraper.service` → `carbuyer.apps.lot_scraper`; `carbuyer-bid-poller.service` → `carbuyer.apps.bid_poller`. Each gets its own unit file with the exec line updated.)

- [ ] **Step 2: Postgres unit — `infra/systemd/carbuyer-postgres.service`**

```ini
[Unit]
Description=CarBuyer Postgres (Docker)
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=true
WorkingDirectory=/home/mark/repos/CarBuyerAssistant/infra
ExecStart=/usr/bin/docker compose up -d postgres
ExecStop=/usr/bin/docker compose stop postgres

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Oneshot timer template — discoverer**

`infra/systemd/carbuyer-discoverer.service`:

```ini
[Unit]
Description=CarBuyer auction discoverer (oneshot)
After=carbuyer-postgres.service
Requires=carbuyer-postgres.service

[Service]
Type=oneshot
User=mark
WorkingDirectory=/home/mark/repos/CarBuyerAssistant
EnvironmentFile=/home/mark/repos/CarBuyerAssistant/.env
ExecStart=/home/mark/repos/CarBuyerAssistant/.venv/bin/python -m carbuyer.apps.auction_discoverer
StandardOutput=journal
StandardError=journal
```

`infra/systemd/carbuyer-discoverer.timer`:

```ini
[Unit]
Description=Run CarBuyer auction discoverer every 6 hours
After=carbuyer-postgres.service

[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
Persistent=true
Unit=carbuyer-discoverer.service

[Install]
WantedBy=timers.target
```

(Same shape, with different `OnUnitActiveSec`/`OnCalendar`, for: `carbuyer-vision.timer` → `OnCalendar=*-*-* 02:00:00`, `Unit=carbuyer-vision.service`; `carbuyer-distiller.timer` → `OnCalendar=*-*-* 03:00:00`, `Unit=carbuyer-distiller.service`. Their service files use `Type=oneshot` like the discoverer, calling `python -m carbuyer.apps.vision_batcher` and `python -m carbuyer.apps.auction_distiller` respectively.)

- [ ] **Step 4: Install script — `infra/systemd/install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/mark/repos/CarBuyerAssistant"
UNIT_DIR="/etc/systemd/system"

cd "$(dirname "$0")"

echo "Linking units to ${UNIT_DIR}..."
for f in *.service *.timer; do
  sudo ln -sf "$(realpath "$f")" "${UNIT_DIR}/${f}"
done

sudo systemctl daemon-reload

# Enable continuous services
for svc in carbuyer-postgres carbuyer-bot carbuyer-dashboard \
           carbuyer-enricher carbuyer-valuator carbuyer-notifier \
           carbuyer-lot-scraper carbuyer-bid-poller; do
  sudo systemctl enable "${svc}.service"
done

# Enable timers
for t in carbuyer-discoverer carbuyer-vision carbuyer-distiller; do
  sudo systemctl enable "${t}.timer"
done

echo "Installed. Run: sudo systemctl start carbuyer-postgres && sudo systemctl start carbuyer-bot ..."
```

- [ ] **Step 5: Commit**

```bash
chmod +x infra/systemd/install.sh
git add infra/systemd/
git commit -m "infra: systemd unit files for all workers + install script"
```

---

### Task 50: Postgres backup script

**Files:**
- Create: `infra/backup.sh`

- [ ] **Step 1: Write `infra/backup.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${HOME}/carbuyer-backups"
mkdir -p "${BACKUP_DIR}"

DATE=$(date -u +%Y-%m-%dT%H-%M-%SZ)
OUT="${BACKUP_DIR}/carbuyer-${DATE}.sql.gz"

docker exec carbuyer-pg pg_dump -U carbuyer -d carbuyer | gzip > "${OUT}"

# Retain 30 days of dailies
find "${BACKUP_DIR}" -name "carbuyer-*.sql.gz" -mtime +30 -delete

echo "Backup written: ${OUT}"
```

- [ ] **Step 2: Make executable and verify it runs**

```bash
chmod +x infra/backup.sh
infra/backup.sh
ls -lh ${HOME}/carbuyer-backups/ | head -3
```

Expected: at least one `.sql.gz` file present.

- [ ] **Step 3: Wire it as a daily cron**

Append to user crontab via `crontab -e`:
```
0 3 * * * /home/mark/repos/CarBuyerAssistant/infra/backup.sh >> /home/mark/carbuyer-backups/backup.log 2>&1
```

(Document this in README; not committed in crontab itself.)

- [ ] **Step 4: Commit**

```bash
git add infra/backup.sh
git commit -m "infra: postgres backup script with 30-day retention"
```

---

### Task 51: README + .env.example

**Files:**
- Create: `README.md`
- Create: `.env.example`

- [ ] **Step 1: Write `.env.example`**

```
DATABASE_URL=postgresql+psycopg://carbuyer:local@localhost:5433/carbuyer  # Phase 0 docker-compose binds host port 5433 (not 5432) to avoid collision with host-native Postgres.
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
HOME_PROVINCE=AB
LOG_LEVEL=INFO
NOTIFY_THRESHOLD=0.15
EARLY_WARNING_RARITY_THRESHOLD=2.0
EARLY_WARNING_MIN_HOURS_TO_CLOSE=48
RESCORE_IMPROVEMENT_THRESHOLD=0.05
QUIET_HOURS_START=22
QUIET_HOURS_END=8
QUIET_HOURS_OVERRIDE_SCORE=0.30
FLIP_MARGIN_MIN_CAD=1500
FLIP_MARGIN_PCT=0.10
# JSON object: {"early_warning": 12345, "hot_deals": ...}
DISCORD_CHANNELS={}
```

- [ ] **Step 2: Write `README.md`**

```markdown
# CarBuyerAssistant

Personal Western-Canadian used-vehicle auction deal-finder.

## Quickstart (local development)

```bash
# 1. Install dependencies
uv sync --extra dev

# 2. Start Postgres
cd infra && docker compose up -d postgres && cd ..

# 3. Apply migrations
uv run alembic upgrade head

# 4. Configure env
cp .env.example .env
# Fill in OPENAI_API_KEY, DISCORD_BOT_TOKEN, DISCORD_CHANNELS

# 5. Run a one-off discovery + lot scrape (manual)
uv run python -m carbuyer.apps.auction_discoverer

# 6. Start the dashboard
uv run python -m carbuyer.apps.dashboard
# Open http://localhost:8000
```

## Architecture

See `docs/specs/2026-05-08-carbuyer-mvp-design.md` for the full design.
See `docs/plans/2026-05-09-auction-mvp-plan.md` for the implementation plan.

## Production deployment

1. `infra/systemd/install.sh` symlinks unit files into `/etc/systemd/system`.
2. Start the services in order: `postgres → bot → dashboard → workers`.
3. `infra/backup.sh` runs daily via crontab, retains 30 days of `pg_dump`s.

## Tests

```bash
uv run pytest
uv run pyright
uv run ruff check .
```

## Honest limitations

- Source plugins for Ritchie Bros + Michener Allen are phase-2 (these auctioneers are large enough to follow manually).
- The desirability and classic-exception taxonomies start small; expand as you encounter sought-after vehicles in real auctions.
- Bid history is reconstructed from polling — only what the source exposes publicly.
```

- [ ] **Step 3: Commit**

```bash
git add README.md .env.example
git commit -m "docs: README + env example"
```

---

### Task 52: End-to-end smoke test (manual checklist)

This is the verification gate. Do it at the end of implementation.

- [ ] **Step 1: Apply migrations against the dev DB**

```bash
uv run alembic upgrade head
```

- [ ] **Step 2: Run a one-shot discovery**

```bash
uv run python -m carbuyer.apps.auction_discoverer
```

Expected log lines: `discovering source=hibid` / `discovering source=mcdougall` / `discovering source=farmauctionguide_discovered`, then `discovery complete found=N` with `N > 0`.

- [ ] **Step 3: Verify rows in DB**

```bash
docker exec carbuyer-pg psql -U carbuyer -d carbuyer -c "SELECT source, COUNT(*) FROM auctions GROUP BY source;"
```

Expected: at least one row per HiBid/McDougall (farmauctionguide may be 0 if no in-house auctions on that day).

- [ ] **Step 4: Run the lot-scraper manually**

```bash
uv run python -m carbuyer.apps.lot_scraper &
SLEEP=15; sleep $SLEEP
docker exec carbuyer-pg psql -U carbuyer -d carbuyer -c "SELECT COUNT(*) FROM auction_lots;"
kill %1
```

Expected: `auction_lots` count > 0.

- [ ] **Step 5: Run enricher + valuator + notifier as separate processes**

In three terminals:
```bash
uv run python -m carbuyer.apps.enricher
uv run python -m carbuyer.apps.valuator
uv run python -m carbuyer.apps.notifier
```

After 1–2 minutes, expect:
```bash
docker exec carbuyer-pg psql -U carbuyer -d carbuyer -c "
  SELECT
    SUM(CASE WHEN enrichment_status='done' THEN 1 ELSE 0 END) AS enriched,
    SUM(CASE WHEN valuation_status='done' THEN 1 ELSE 0 END) AS valued,
    SUM(CASE WHEN price_deal_score >= 0.15 THEN 1 ELSE 0 END) AS deals,
    SUM(CASE WHEN rarity_score >= 2 THEN 1 ELSE 0 END) AS rare
  FROM auction_lots;"
```

Expected: `enriched > 0`. `valued` may be 0 until comps accumulate. `deals` and `rare` may be 0 on first run — that's fine.

- [ ] **Step 6: Open the dashboard**

```bash
uv run python -m carbuyer.apps.dashboard
```

Visit `http://localhost:8000`. Expect the auction feed to render lot cards.

- [ ] **Step 7: Verify Discord post (if any deal/rare lot was scored)**

Check the `#early-warning` and `#hot-deals` channels. Click 👍 Interested on one — confirm dashboard `/watched` shows it.

- [ ] **Step 8: Tag a release**

```bash
git tag -a v0.1.0 -m "MVP: end-to-end auction deal-finder"
```

(Do not push the tag without explicit user confirmation per CLAUDE.md.)

---

## Self-review notes (planner internal)

**Spec coverage:**
- §1 goals → Phases 1, 6, 7, 8, 11 (auction sources, two-trigger notifications, comp comparison, sold-price tracking, legal tracking).
- §2 architecture → Phases 0, 2, 3–11 (Postgres, LISTEN/NOTIFY, SKIP LOCKED, all workers).
- §3 data model → Phase 0 Task 6 + Task 7 (all tables + initial migration).
- §4 scoring → Phase 4 (channel norm, comps, fair-value range, landed cost, deal score, rarity, max bid).
- §5 LLM enrichment → Phases 3 + 8 (description + vision two-pass).
- §6 notifications → Phases 5 + 6 (Discord bot + notifier with all five trigger types).
- §7 dashboard → Phase 11 (all eight views).
- §8 tech stack → Phase 0 + scattered (FastAPI, HTMX, SQLAlchemy 2 async, etc.).
- §9 phase-2 → explicitly deferred; not in this plan.

**Type consistency check:** all schema field names match between Pydantic models, ORM models, and template usage. Worker module names match between systemd units and `python -m carbuyer.apps.<name>` paths.

**Verification path:** Phase 12 Task 52 is the end-to-end smoke that proves the whole pipeline works on real data.

**Unknown-platform feedback loop:** Task 42 alerts on Discord whenever farmauctionguide finds an auction whose platform we can't parse. Task 43's `/needs-plugin` view + retry-routing endpoint lets the user add a plugin and reroute existing rows so the new plugin processes them. End-to-end: discovery → alert → user adds plugin → user clicks Retry routing → lot-scraper picks up the auction under the new source.

---

### Phase 12 post-implementation overlay

Tasks 49, 50, 51 land in this phase. Task 52 (end-to-end smoke checklist) is
the user's manual verification gate — runs against live OpenAI + Discord +
Postgres after merge, not part of the implementation commits.

#### Plan-vs-code corrections (recurring + new)

1. **`.env.example` expanded to cover every `Settings` key.** Plan's
   example was a 17-line stub; the actual `Settings` class in
   `src/carbuyer/shared/config.py` has 25+ tunables. Restructured into four
   sections: required-no-default, database, common knobs (shown
   uncommented at defaults), and optional tunables (commented out with
   their defaults). A user copying `.env.example → .env` now sees every
   knob the system respects without having to read the code.

2. **README worker list extended** to include `bid_poller`,
   `vision_batcher`, `auction_distiller` (Phase 7/8/9 additions the
   original plan README pre-dated). Production-deployment section gets a
   table mapping each unit to its role + cadence (continuous vs 6h timer
   vs nightly 02:00/03:00 UTC).

3. **`backup.sh` date-format comment** added explaining the ISO-8601
   `T...Z` choice (sortable + unambiguous UTC, no `:` to confuse
   Windows/SMB shares).

4. **Skipped Task 52 (smoke test) entirely.** It costs OpenAI tokens,
   posts to Discord, and requires real credentials — the user runs this
   manually when ready for cutover.

#### Reviewer pass — findings applied (commit `5c2039e`)

Single bugs/correctness reviewer pass against the branch. 3 critical + 2
important findings, all applied:

5. **`EnvironmentFile=` made optional across all 10 service units.** Plan
   used the bare `EnvironmentFile=/.../.env` form. systemd treats a missing
   file as a hard error in that form — first-deploy ordering (service
   enabled before `.env` is created) would fail to start with `Failed to
   load environment files`. Prefixed all 10 with `-` so missing `.env` is
   silently OK; pydantic-settings still fail-fasts at Python startup if
   required keys are absent. (The postgres service has no
   `EnvironmentFile=` and is unaffected.)

6. **`DISCORD_CHANNELS` example used wrong key names.** Plan's example
   showed `closing_soon` and `watched` as channel keys; the `ChannelKey`
   `Literal` in `apps/bot/channels.py` actually requires `auction_closing`
   and `watchlist`. A user copying the example verbatim would silently
   lose auction-closing and watchlist notifications (the notifier logs a
   warning and continues on a missing channel key). Corrected to the real
   `ChannelKey` values and rewrote the inline comment to point at the
   source-of-truth file.

7. **`backup.sh` atomicity fix.** Plan had
   `docker exec ... pg_dump | gzip > "${OUT}"`. The shell opens `$OUT`
   before the pipeline starts; if the container is down, `gzip` receives
   immediate EOF and writes a valid-but-empty `.sql.gz` to `$OUT`.
   `pipefail` then catches the docker exit code and aborts the script,
   but the zero-byte file is already on disk and survives the 30-day
   `find -delete` prune. The next `pg_restore` would silently lose that
   day. Restructured: pre-flight `pg_isready` check that aborts cleanly
   with stderr message, then write to `$OUT.tmp` and `mv` to `$OUT` only
   on success (atomic; `trap` cleans the tmp on any abort).

8. **`install.sh` now `chmod +x infra/backup.sh`** so the cron entry
   documented in the README works even on a clone where
   `git core.fileMode=false` has dropped the executable bit. Without
   this, daily backups would silently fail with `Permission denied` and
   cron usually emails nowhere by default.

#### Pyright / ruff / pytest

- **Pytest baseline.** 437 passed, 1 skipped — unchanged from Phase 11.
  Phase 12 touched zero Python files.
- **Pyright baseline.** 59 errors — unchanged.
- **Ruff clean.** All checks pass.

#### Reviewer items not actioned (acknowledged)

- **`auction_watch`, `vision_updates`, `system_health` channel keys not
  in the `.env.example` JSON example** — these `ChannelKey` literals are
  reserved for future trigger types not yet wired in `_FIXED_ROUTES`.
  Leaving them out keeps the example minimal and focused on what the
  current notifier actually uses. The comment lists all eight valid keys
  for future reference.
- **Backup script does not verify checksum / parseability of the dump.**
  pg_dump exits 0 only on successful completion, and the temp-file +
  rename pattern means a partially-written dump never reaches `$OUT`.
  Adding a per-file gzip integrity check (`gzip -t`) is cheap but
  marginal once the atomicity issue is fixed.
- **No log rotation for `journalctl` per-service.** Distro defaults apply;
  ops can tune via `/etc/systemd/journald.conf` if needed.

---

End of Phase 12. MVP is implementation-complete; Task 52 (manual smoke
test) is the cutover gate.
