# Using CarBuyerAssistant

## What this is
A 10-process Python pipeline that ingests vehicle lots from Western Canadian online auctions (HiBid, McDougall), enriches and scores them with an LLM, polls bids on a soft-close-aware cadence, and posts Discord alerts for rare finds and closing-cheap lots. Postgres is both source-of-truth and queue (NOTIFY/LISTEN + `SKIP LOCKED`); a FastAPI/HTMX dashboard lets the operator browse and flag lots.

## Prerequisites
- Docker + Docker Compose (for the Postgres 17 container)
- `uv` >= 0.11 (the project pins Python 3.13 in production; 3.12 works for local dev — `requires-python = ">=3.12"`)
- Host port 5433 free for Postgres (compose binds `127.0.0.1:5433:5432`)
- Outbound network (optional) for the LLM stage, Discord webhooks, and live HiBid GraphQL — none required for dashboard / migrations / tests

## First-time setup
1. Start Postgres: `docker compose -f infra/docker-compose.yml up -d postgres`
2. Install Python deps: `uv sync --extra dev`
3. Create local env: `cp .env.example .env`, then **set `DISCORD_GUILD_ID=0`** (an empty value fails pydantic int parsing — see Known issues). Leave `OPENAI_API_KEY` / `DISCORD_BOT_TOKEN` blank for local-only use.
4. Apply schema: `uv run alembic upgrade head` (created 9 tables on a fresh DB through revision `a7d3a0c1e927`)
5. Build dashboard CSS: `make css` (downloads `bin/tailwindcss` v4.3 if missing, then compiles to `app.css`)

## Run it
The dashboard is the only process that runs cleanly without external credentials.

```
# Note: the __main__ entrypoint hardcodes port 8000. If that's taken, invoke
# uvicorn directly with a different port:
uv run uvicorn carbuyer.apps.dashboard.app:app --host 127.0.0.1 --port 8765
```

Workers can be smoke-started individually; they fail fast (non-zero exit, clear log) when their required credentials are missing. Verified locally:
- `uv run python -m carbuyer.apps.notifier` → exits with `DISCORD_BOT_TOKEN not configured`
- `uv run python -m carbuyer.apps.enricher` → exits with `OPENAI_API_KEY not configured`
- `uv run python -m carbuyer.apps.valuator` → starts the listener loop (no external deps until rows arrive)

For the full pipeline you need real OpenAI + Discord credentials and the systemd units in `infra/systemd/` (production-only — `install.sh` is SELinux-aware and assumes Fedora).

## Try it out
1. `curl -s http://127.0.0.1:8765/` returns `<title>Today — CarBuyer</title>` and a "No lots" empty state on a fresh DB.
2. Open `http://127.0.0.1:8765/` in a browser to see the Today / Watchlist / Recent / Closing tabs.
3. To inject a synthetic lot without scraping HiBid, insert directly:
   ```
   PGPASSWORD=local psql -h 127.0.0.1 -p 5433 -U carbuyer -d carbuyer
   ```
   then `INSERT` into `auctions` and `auction_lots` (schema in `src/carbuyer/db/models.py`). The valuator/notifier/dashboard pick rows up via NOTIFY.
4. Tests: `uv run pytest -q` — verified locally, 544 passed, 1 timezone-dependent flake, 1 skipped, ~7s.

## Known issues / gotchas
- **Empty `DISCORD_GUILD_ID=` in `.env` crashes every entrypoint** at import time (`Settings.discord_guild_id: int | None = None` but pydantic-settings parses the empty string as `""` and fails int validation). Workaround: set `DISCORD_GUILD_ID=0`. The example file ships with an empty value, so this bites on first run.
- **Dashboard port is hardcoded to 8000** in `src/carbuyer/apps/dashboard/__main__.py`. No env override. Use `uvicorn` directly if 8000 is taken.
- **Test `tests/apps/dashboard/test_closing_buckets_bin_by_time` is timezone-flaky** — fails when local UTC offset pushes a `+1d2h` lot out of the "tomorrow" bucket. The test comment acknowledges the boundary case but still asserts.
- **README quickstart still references `cd infra && docker compose up -d postgres`** — works, but `docker compose -f infra/docker-compose.yml ...` from the repo root is equivalent and avoids the cd dance.
- **README claims Python 3.13** but `pyproject.toml` accepts `>=3.12`; 3.12.3 worked end-to-end for everything verified above.
- Did **not** verify: live HiBid GraphQL ingestion, Discord bot, LLM stages, vision batcher, systemd install (Fedora-only).

## Stop / cleanup
```
# Stop dashboard (if running on 8765)
pkill -f "uvicorn carbuyer.apps.dashboard"

# Leave Postgres running for next session, OR stop it:
docker compose -f infra/docker-compose.yml stop postgres

# Full reset (destroys the local DB volume):
docker compose -f infra/docker-compose.yml down -v
```

Postgres container `carbuyer-pg` is left **running** at end of this verification session.
