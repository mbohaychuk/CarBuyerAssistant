#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/markbohaychuk/repos/CarBuyerAssistant"
UNIT_DIR="/etc/systemd/system"

cd "$(dirname "$0")"

# Ensure the backup script is executable. git core.fileMode=false (sometimes
# set on cross-platform clones) drops the executable bit, which makes a daily
# cron entry silently fail with "Permission denied" — and cron usually emails
# nowhere by default.
chmod +x "${REPO_DIR}/infra/backup.sh"

echo "Linking units to ${UNIT_DIR}..."
for f in *.service *.timer; do
  sudo ln -sf "$(realpath "$f")" "${UNIT_DIR}/${f}"
done

sudo systemctl daemon-reload

for svc in carbuyer-postgres carbuyer-bot carbuyer-dashboard \
           carbuyer-enricher carbuyer-valuator carbuyer-notifier \
           carbuyer-bid-poller; do
  sudo systemctl enable "${svc}.service"
done

# carbuyer-lot-scraper is symlinked but NOT auto-enabled — the lot-first
# ingester replaces it for HiBid (the only currently-working source).
# Manually enable lot-scraper only when reviving farmauctionguide/mcdougall
# sources that still use the old discover→scrape pattern.
# carbuyer-discoverer is likewise symlinked but not enabled — the new
# ingester is the canonical discovery+scrape worker.
for t in carbuyer-ingester carbuyer-vision carbuyer-distiller; do
  sudo systemctl enable "${t}.timer"
done

echo "Installed. Run: sudo systemctl start carbuyer-postgres && sudo systemctl start carbuyer-bot ..."
