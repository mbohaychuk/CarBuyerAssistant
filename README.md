# CarBuyerAssistant

A personal deal-finder for Western Canadian used-vehicle auctions. Aggregates lots from farm and estate auctions, scores them with an LLM, watches bids through soft-close, and pushes two kinds of Discord alerts: "rare vehicle, you've got days to drive out and look" and "this lot is closing cheap right now."

The deals are at small regional auctions. Kijiji's deal density collapsed years ago, AutoTrader is dealer markup, Facebook Marketplace is hostile to scraping. Farm dispersal sales, estate auctions, and provincial heavy-equipment auctioneers are where late-model trucks still cross the block at a third of retail — but their catalogs are scattered across a dozen sites with no API, soft-close timers, and listings that are gone the moment the hammer drops. This system is the consolidator that those auctioneers will never build.

## Architecture

Staged pipeline of independent worker processes sharing one Postgres. Each stage NOTIFY/LISTEN-signals the next; failure of any worker doesn't cascade.

```
                            ┌──────────────────┐
                            │  PostgreSQL 17   │   source of truth + queue +
                            │  + pg_advisory   │   pub/sub via LISTEN/NOTIFY
                            └────────┬─────────┘
                                     │
       ┌──────────────────┬──────────┴──────────┬───────────────────┐
       ▼                  ▼                     ▼                   ▼
 ┌────────────┐    ┌────────────┐         ┌────────────┐     ┌────────────┐
 │ ingester   │    │ enricher   │         │ valuator   │     │ notifier   │
 │ (HiBid     │───▶│ (LLM       │────────▶│ (comps +   │────▶│ (Discord   │
 │  GraphQL,  │    │  describe, │ NOTIFY  │  fair      │     │  embeds)   │
 │  6h timer) │    │  classify) │         │  value)    │     │            │
 └────────────┘    └────────────┘         └────────────┘     └────────────┘
                                                ▲
                          ┌──────────────────┐  │
                          │ bid_poller       │──┤  re-runs valuation on every
                          │ (tiered cadence) │  │  bid change so price-deal
                          └──────────────────┘  │  score reflects reality
                                                │
                          ┌──────────────────┐  │
                          │ vision_batcher   │──┘  nightly: condition
                          │ (nightly 02:00)  │     check on shortlist
                          └──────────────────┘
                          ┌──────────────────┐
                          │ auction_         │     nightly: distill closed
                          │ distiller        │     lots into historical_sales
                          │ (nightly 03:00)  │
                          └──────────────────┘

                          ┌──────────────────┐    ┌──────────────────┐
                          │ dashboard        │    │ Discord bot      │
                          │ (FastAPI + HTMX) │    │ (slash commands  │
                          │ — flag lots,     │    │  flag, watchlist,│
                          │   side-by-side   │    │  ack alerts)     │
                          │   comp browser   │    │                  │
                          └──────────────────┘    └──────────────────┘
```

Ten processes, all systemd-managed. Three are timers (ingester, vision, distiller); seven run continuously with `Restart=always`. Logs go to journald.

## Design decisions

### Postgres NOTIFY/LISTEN as the queue

A dedicated `psycopg3` async connection runs `LISTEN enrichment_pending`, `LISTEN valuation_pending`, etc. The upstream worker writes the row, calls `NOTIFY <channel>`, and the downstream worker wakes up and claims work via `SELECT ... FOR UPDATE SKIP LOCKED`. The same connection that claims also commits — no separate worker registry.

Redis or RabbitMQ would have been the textbook choice. Both add a moving part to operate, monitor, and back up. Postgres NOTIFY hands all of that to the database I'm already running, and `SKIP LOCKED` makes the claim/process/commit pattern safe under concurrent consumers even though, in practice, each stage runs as a single instance.

The cost is that NOTIFY payloads are limited (~8KB), so the messages only carry row IDs — claiming requires a follow-up query. For this system that's fine; the queue depth never exceeds a few hundred and the latency is dominated by LLM calls anyway.

### Single-instance enforcement via advisory locks

Each continuous worker (notifier, enricher, valuator, bid_poller) and the one-shot ingester open a dedicated psycopg connection at startup and run `pg_try_advisory_lock(hashtext("notifier"))`. The lock is held for the process lifetime; closing the connection releases it.

The `SELECT FOR UPDATE SKIP LOCKED` claim pattern and the `recover_orphans` catchup sweep both assume no concurrent claimer exists. A second instance — typically an operator running `python -m carbuyer.apps.notifier` from a shell while the systemd unit is also alive — would produce duplicate Discord posts and double-processed enrichments.

If the lock is taken, the worker logs an error and exits non-zero. systemd's `Restart=always` cycles it; if the real peer is still alive, the next retry also fails. That's the desired symptom — a noisy loop, not a corrupted database.

See `src/carbuyer/shared/singleton.py`.

### HiBid: GraphQL behind Cloudflare, not the HTML pages

HiBid migrated its catalog to a SPA in early 2026. The province auction-list page is still server-rendered, so discovery scrapes `<a href>` patterns there. But the individual auction catalog pages are empty shells — lot data arrives via GraphQL POSTs to `/graphql` (`AuctionDetails`, `LotSearchLotOnly`, `CategorySearch`).

The ingester rebuilds those queries directly. Bid polling reuses `LotSearchLotOnly` with `eventItemIds=[id]` to fetch a single lot. Every GraphQL POST needs a Cloudflare-issued `__cf_bm` cookie, so the client bootstraps by hitting any HiBid page once per lifetime and rides the same cookie jar.

The brittle alternative would have been driving the SPA with Playwright. The GraphQL contract changes less often than the HTML rendering does, and it's an order of magnitude faster.

### Tiered bid-polling cadence (soft-close aware)

HiBid auctions soft-close: a bid in the last 60 seconds extends the lot. Polling at a fixed cadence either burns requests on lots that aren't close to closing or misses the action when one is. The bid_poller keeps a priority queue keyed on `next_poll_at`:

| Time to close | Cadence |
|---|---|
| > 24h | 60 min |
| 2–24h | 15 min |
| 1–2h | 5 min |
| 10–60 min | 60 s |
| < 10 min or extended | 30 s |

Polling continues past the nominal end time until the source reports `lot_status=closed`. This is what reconstructs the final sale price for `historical_sales`, which feeds the next valuation pass.

### Lot-first ingest, not auction-then-scrape

The original design had two stages: a discoverer that found new auctions, and a lot-scraper that walked each auction's catalog. After HiBid's SPA migration the cross-auction `LotSearchLotOnly` GraphQL query made one-shot lot ingestion strictly better — fewer requests, fewer parse paths, lots from across all open auctions in a single response. The McDougall plugin uses the same lot-first pattern via its `products.php?category=Vehicles` catalog. The legacy auction-then-scrape workers were retired entirely; the per-source upsert helpers live in `carbuyer.db.upserts` and the ingester dispatches one strategy per source.

### SELinux-aware systemd install

The install script copies unit files into `/etc/systemd/system` rather than symlinking, then `restorecon`s the repo to `bin_t`. Symlinked units on a Fedora host pick up `user_home_t` and systemd refuses to start them; copying + relabel is the only path that works without disabling SELinux. The script is idempotent.

The hardening stanza on every worker unit is the baseline kit: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `MemoryDenyWriteExecute`. The repo path is whitelisted via `ReadWritePaths=` so Python's bytecode cache works.

### Postgres in Docker, services on the host

The Postgres container is owned by a `carbuyer-postgres.service` oneshot wrapper. All workers connect to `localhost:5433` (a non-default port, because the laptop also runs the regular pg cluster on 5432). Running Postgres in Docker keeps the database upgrade path independent of the host distribution; running the workers on the host avoids the IPC + filesystem complexity of containerizing each one.

### Sources are a plugin registry

`carbuyer.sources.base.AuctionSource` is an ABC. Each source module — `sources/hibid/source.py`, future `sources/mcdougall.py`, etc. — implements it and decorates itself with `@register` at import time. The ingester loads all registered sources and walks them in parallel. Adding a new auctioneer is a single module; no orchestration changes.

## Tech stack

| Component | Technology | Why |
|---|---|---|
| Database | PostgreSQL 17 (Docker) | NOTIFY/LISTEN, SKIP LOCKED, advisory locks, JSON. The queue *and* the source of truth. |
| Async runtime | Python 3.13, asyncio | LLM calls and HTTP scraping are both IO-bound. |
| ORM / migrations | SQLAlchemy 2 (async) + Alembic | Typed sessions, autogenerated migrations. |
| Notifications | psycopg3 `AsyncConnection` | The sync SQLAlchemy session pool can't hold an open LISTEN. |
| LLM | OpenAI gpt-5-nano (text + vision) | Single multimodal model; cost target under $20/month. |
| HTTP scraping | httpx + jittered cadence | Residential IP, conservative pacing. |
| Dashboard | FastAPI + HTMX | Server-rendered, no SPA build step. |
| Discord | discord.py | Slash commands for flag/watchlist/ack. |
| Process supervision | systemd | Restart=always, timers, hardening, journald logs. |
| Backups | `pg_dump` via cron, 30-day retention | One bash script, no third-party backup tool. |

## Quickstart (local development)

```bash
# 1. Install dependencies
uv sync --extra dev

# 2. Start Postgres (container on port 5433)
cd infra && docker compose up -d postgres && cd ..

# 3. Apply migrations
uv run alembic upgrade head

# 4. Configure env
cp .env.example .env
# Fill in OPENAI_API_KEY, DISCORD_BOT_TOKEN, DISCORD_CHANNELS

# 5. Run any worker as a one-off (each is a runnable module)
uv run python -m carbuyer.apps.ingester
uv run python -m carbuyer.apps.enricher
uv run python -m carbuyer.apps.valuator
uv run python -m carbuyer.apps.notifier
uv run python -m carbuyer.apps.bid_poller
uv run python -m carbuyer.apps.vision_batcher
uv run python -m carbuyer.apps.auction_distiller

# 6. Start the dashboard
uv run python -m carbuyer.apps.dashboard
# Open http://localhost:8000
```

## Production deployment

`infra/systemd/install.sh` copies unit files into `/etc/systemd/system`, `restorecon`s them, runs `daemon-reload`, and enables the continuous services + timers. After install, start them in order: `postgres → bot → dashboard → workers`.

| Unit                          | Role                       | Cadence                     |
| ----------------------------- | -------------------------- | --------------------------- |
| `carbuyer-postgres.service`   | Postgres (Docker)          | continuous (oneshot wrap)   |
| `carbuyer-bot.service`        | Discord bot                | continuous                  |
| `carbuyer-dashboard.service`  | HTTP dashboard             | continuous                  |
| `carbuyer-enricher.service`   | LLM enrichment worker      | continuous                  |
| `carbuyer-valuator.service`   | Valuation worker           | continuous                  |
| `carbuyer-notifier.service`   | Discord notifier           | continuous                  |
| `carbuyer-bid-poller.service` | Bid poller                 | continuous                  |
| `carbuyer-ingester.timer`     | Multi-source lot ingester  | every 6h (10min after boot) |
| `carbuyer-vision.timer`       | Nightly vision batch       | daily 02:00 UTC             |
| `carbuyer-distiller.timer`    | Nightly distiller          | daily 03:00 UTC             |
| `carbuyer-source-watchdog.timer` | Stale-source alerter    | hourly                      |

`infra/backup.sh` runs daily via crontab and retains 30 days of `pg_dump`s:

```
0 3 * * * /home/markbohaychuk/repos/CarBuyerAssistant/infra/backup.sh >> /home/markbohaychuk/carbuyer-backups/backup.log 2>&1
```

### Hardening: next step

Services currently run as `User=mark` — the developer's interactive login account. To reduce blast radius, create a dedicated system user and relocate the repo:

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/carbuyer carbuyer
sudo rsync -a /home/markbohaychuk/repos/CarBuyerAssistant/ /opt/carbuyer/
sudo chown -R carbuyer:carbuyer /opt/carbuyer
# Then sed-update User=mark → User=carbuyer and the path references in
# infra/systemd/*.service and re-run install.sh.
```

Until then, a process compromise inside any worker can read the operator's `~/.ssh`, `~/.gnupg`, and `~/.config`. Write is already blocked by `ProtectHome=read-only`.

## Tests

```bash
uv run pytest        # unit + integration
uv run pyright       # strict mode
uv run ruff check .  # linting
```

## Honest limitations

- **Source coverage.** HiBid and McDougall are the live sources. FarmAuctionGuide was built as a routing aggregator, validated, then removed when its per-province pages went behind Cloudflare and its content shifted toward auctioneer-owned sites rather than platform-hosted catalogs; long-tail auctioneer discovery moved to an operator-driven workflow. Ritchie Bros and Michener Allen are phase-2 — both are large enough that manual browsing isn't a hardship.
- **Comp data depth.** The `historical_sales` table grows from auction outcomes the system observes. Bootstrapping took a few weeks; valuations on rare vehicles are still noisy until the comp set fills in.
- **Bid history is reconstructed from polling.** Only what the source exposes publicly — no proxy-bid visibility, no buyer identity.
- **Desirability and classic-exception taxonomies are small.** Expanded as I encounter sought-after vehicles in real auctions; this is intentionally slow growth.
- **Single-user.** No multi-user accounts, no shared watchlists. This is a personal tool.
