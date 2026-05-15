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

| Unit                          | Role                                  | Cadence                   |
| ----------------------------- | ------------------------------------- | ------------------------- |
| `carbuyer-postgres.service`   | Postgres (Docker)                     | continuous (oneshot wrap) |
| `carbuyer-bot.service`        | Discord bot                           | continuous                |
| `carbuyer-dashboard.service`  | HTTP dashboard                        | continuous                |
| `carbuyer-enricher.service`   | LLM enrichment worker                 | continuous                |
| `carbuyer-valuator.service`   | Valuation worker                      | continuous                |
| `carbuyer-notifier.service`   | Discord notifier                      | continuous                |
| `carbuyer-bid-poller.service` | Bid poller                            | continuous                |
| `carbuyer-ingester.timer`     | HiBid lot-first ingester              | every 6h (10min after boot) |
| `carbuyer-vision.timer`       | Nightly vision batch                  | daily 02:00 UTC           |
| `carbuyer-distiller.timer`    | Nightly distiller                     | daily 03:00 UTC           |

The `lot-scraper.service` and `discoverer.timer` unit files ship in
`infra/systemd/` but are NOT auto-enabled by `install.sh` — they implement
the legacy auction-then-scrape pattern that's superseded by `ingester` for
HiBid (the only currently-working source). Enable them manually if you
ever revive the farmauctionguide/mcdougall plugins (currently 404'd).

`infra/backup.sh` runs daily via crontab and retains 30 days of `pg_dump`s.
Suggested crontab entry:

```
0 3 * * * /home/markbohaychuk/repos/CarBuyerAssistant/infra/backup.sh >> /home/markbohaychuk/carbuyer-backups/backup.log 2>&1
```

### Single-instance enforcement

Each continuous worker (notifier, enricher, valuator, bid_poller,
lot_scraper) acquires a Postgres advisory lock via `pg_try_advisory_lock`
at startup, held by a dedicated psycopg connection for the process
lifetime. If the lock is already taken — typically because an operator
ran `python -m carbuyer.apps.notifier` from a shell while the systemd
unit was also active — the worker logs an error and exits non-zero;
`Restart=always` will keep cycling until the contention clears. This
exists because the SELECT FOR UPDATE SKIP LOCKED claim plus
`recover_orphans` catchup-sweep both assume no concurrent claimer, and
duplicate workers would produce duplicate Discord posts.

See `src/carbuyer/shared/singleton.py` for the implementation.

### Hardening

The worker units ship with a baseline sandbox stanza (`NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`,
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `MemoryDenyWriteExecute`,
no extra capabilities). The repo path is whitelisted via `ReadWritePaths=`
so Python's bytecode cache works; everything else under `/home` is read-only.

**Next step (not yet automated):** services currently run as `User=mark`,
the developer's interactive login account. To reduce blast radius further,
create a dedicated system user and relocate the repo:

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/carbuyer carbuyer
sudo rsync -a /home/markbohaychuk/repos/CarBuyerAssistant/ /opt/carbuyer/
sudo chown -R carbuyer:carbuyer /opt/carbuyer
# Then sed-update User=markbohaychuk → User=carbuyer and the path references in
# infra/systemd/*.service and re-run install.sh.
```

Until then, a process compromise inside any worker can read the operator's
`~/.ssh`, `~/.gnupg`, and `~/.config` (write is blocked by `ProtectHome`).

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
