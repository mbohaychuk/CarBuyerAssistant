#!/usr/bin/env bash
# Install and start the CarBuyer systemd unit fleet. Re-runnable: every
# action below is idempotent so this script also works as a "bring
# everything up" after a reboot or a manual `systemctl stop`.
set -euo pipefail

REPO_DIR="/home/markbohaychuk/repos/CarBuyerAssistant"
cd "${REPO_DIR}/infra/systemd"

echo "=== 1/4 install (symlink units + enable for boot) ==="
sudo bash install.sh

echo
echo "=== 2/4 starting continuous services ==="
# postgres first because every worker depends on it; bot before notifier
# only by convention (notifier uses Discord REST, not the gateway, so the
# bot service is independent in practice).
sudo systemctl start carbuyer-postgres.service
sudo systemctl start carbuyer-bot.service
sudo systemctl start carbuyer-dashboard.service
sudo systemctl start \
  carbuyer-enricher.service \
  carbuyer-valuator.service \
  carbuyer-notifier.service \
  carbuyer-bid-poller.service

echo
echo "=== 3/4 starting timers ==="
sudo systemctl start \
  carbuyer-ingester.timer \
  carbuyer-vision.timer \
  carbuyer-distiller.timer

echo
echo "=== 4/4 verifying ==="
echo
echo "-- Continuous services --"
for svc in postgres bot dashboard enricher valuator notifier bid-poller; do
  state=$(systemctl is-active "carbuyer-${svc}.service" 2>&1 || true)
  printf "  %-14s %s\n" "${svc}" "${state}"
done

echo
echo "-- Timers (next-firing) --"
systemctl --no-pager list-timers 'carbuyer-*.timer' | head -8 || true

cat <<MSG

Setup complete. Useful commands:

  Watch a worker live:
    journalctl -u carbuyer-notifier.service -f

  Fire the ingester immediately (instead of waiting ~10 min):
    sudo systemctl start carbuyer-ingester.service

  Stop everything:
    sudo systemctl stop 'carbuyer-*.service' 'carbuyer-*.timer'

  Disable autostart on boot:
    sudo systemctl disable 'carbuyer-*.service' 'carbuyer-*.timer'

  Status all-units summary:
    systemctl --no-pager list-units 'carbuyer-*'

  Dashboard:
    open http://localhost:8000
MSG
