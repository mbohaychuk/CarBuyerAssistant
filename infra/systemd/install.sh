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

echo "Installing units to ${UNIT_DIR}..."
# `install -m 0644` (not `ln -sf`) so the destination gets the right
# SELinux context (`systemd_unit_file_t`). Symlinks into `/home` inherit
# `user_home_t`, which systemctl refuses to read on Fedora/RHEL, surfacing
# as "Failed to enable unit: Access denied". This means edits in the repo
# require a re-run of install.sh to take effect — acceptable since unit
# files change rarely.
for f in *.service *.timer; do
  # Remove any stale symlink (left over from old `ln -sf`-style installs)
  # before copying — `install` won't overwrite a symlink without -T, but
  # we want a clean regular file at the destination either way.
  if [[ -L "${UNIT_DIR}/${f}" ]]; then
    sudo rm -f "${UNIT_DIR}/${f}"
  fi
  sudo install -m 0644 -T "$f" "${UNIT_DIR}/${f}"
done

sudo systemctl daemon-reload

# SELinux relabel — required on Fedora/RHEL so systemd's init_t context
# can read+execute the python binary, source files, and .env under our
# repo. Files in /home default to `user_home_t` which init_t can't read,
# manifesting as "ExecStart fails to launch" or SELinux notification
# pop-ups. Labeling the repo tree as bin_t makes it readable + executable
# by system services. We `semanage` to make it persistent across
# `restorecon` runs and reboots, then apply with `restorecon`.
if command -v semanage &>/dev/null && getenforce 2>/dev/null | grep -qv "Disabled"; then
  echo "SELinux relabel of ${REPO_DIR} (one-time persistent rule)..."
  # semanage -a is not idempotent: it errors if the rule already exists.
  # Delete-then-add is the idempotent pattern.
  sudo semanage fcontext -d "${REPO_DIR}(/.*)?" 2>/dev/null || true
  sudo semanage fcontext -a -t bin_t "${REPO_DIR}(/.*)?"
  sudo restorecon -RF "${REPO_DIR}"
fi

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
