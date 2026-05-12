#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${HOME}/carbuyer-backups"
mkdir -p "${BACKUP_DIR}"

# ISO 8601 basic with `T` and `Z` so filenames sort lexicographically and
# the UTC timezone is unambiguous (no `:` to confuse Windows/SMB shares).
DATE=$(date -u +%Y-%m-%dT%H-%M-%SZ)
OUT="${BACKUP_DIR}/carbuyer-${DATE}.sql.gz"
TMP="${OUT}.tmp"

# Fail loud if Postgres isn't reachable. Without this, a stopped container
# leaves an empty .sql.gz at $OUT that looks like a valid backup and survives
# the 30-day retention prune — the next restore attempt would silently lose
# the day. pipefail catches docker's exit code but only AFTER the empty file
# has been written.
if ! docker exec carbuyer-pg pg_isready -U carbuyer -d carbuyer >/dev/null 2>&1; then
  echo "carbuyer-pg container is not running or Postgres is not ready" >&2
  exit 1
fi

# Write to .tmp and rename on success so a partial dump never appears under
# the canonical filename. The trap removes the tmp on any abort.
trap 'rm -f "${TMP}"' EXIT
docker exec carbuyer-pg pg_dump -U carbuyer -d carbuyer | gzip > "${TMP}"
mv "${TMP}" "${OUT}"
trap - EXIT

find "${BACKUP_DIR}" -name "carbuyer-*.sql.gz" -mtime +30 -delete

echo "Backup written: ${OUT}"
