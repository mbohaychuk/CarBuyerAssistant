#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${HOME}/carbuyer-backups"
mkdir -p "${BACKUP_DIR}"

# ISO 8601 basic with `T` and `Z` so filenames sort lexicographically and
# the UTC timezone is unambiguous (no `:` to confuse Windows/SMB shares).
DATE=$(date -u +%Y-%m-%dT%H-%M-%SZ)
OUT="${BACKUP_DIR}/carbuyer-${DATE}.sql.gz"

docker exec carbuyer-pg pg_dump -U carbuyer -d carbuyer | gzip > "${OUT}"

find "${BACKUP_DIR}" -name "carbuyer-*.sql.gz" -mtime +30 -delete

echo "Backup written: ${OUT}"
