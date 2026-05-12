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

# 5. Run any worker as a one-off (each is a runnable module)
uv run python -m carbuyer.apps.auction_discoverer
uv run python -m carbuyer.apps.lot_scraper
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

## Architecture

See `docs/specs/2026-05-08-carbuyer-mvp-design.md` for the full design.
See `docs/plans/2026-05-09-auction-mvp-plan.md` for the implementation plan.

## Production deployment

`infra/systemd/install.sh` symlinks unit files into `/etc/systemd/system`,
runs `daemon-reload`, and enables continuous services + timers. After
install, start them in order: `postgres -> bot -> dashboard -> workers`.

| Unit                          | Role                  | Cadence                   |
| ----------------------------- | --------------------- | ------------------------- |
| `carbuyer-postgres.service`   | Postgres (Docker)     | continuous (oneshot wrap) |
| `carbuyer-bot.service`        | Discord bot           | continuous                |
| `carbuyer-dashboard.service`  | HTTP dashboard        | continuous                |
| `carbuyer-enricher.service`   | LLM enrichment worker | continuous                |
| `carbuyer-valuator.service`   | Valuation worker      | continuous                |
| `carbuyer-notifier.service`   | Discord notifier      | continuous                |
| `carbuyer-lot-scraper.service`| Lot scraper           | continuous                |
| `carbuyer-bid-poller.service` | Bid poller            | continuous                |
| `carbuyer-discoverer.timer`   | Auction discoverer    | every 6h (10min after boot) |
| `carbuyer-vision.timer`       | Nightly vision batch  | daily 02:00 UTC           |
| `carbuyer-distiller.timer`    | Nightly distiller     | daily 03:00 UTC           |

`infra/backup.sh` runs daily via crontab and retains 30 days of `pg_dump`s.
Suggested crontab entry:

```
0 3 * * * /home/mark/repos/CarBuyerAssistant/infra/backup.sh >> /home/mark/carbuyer-backups/backup.log 2>&1
```

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
