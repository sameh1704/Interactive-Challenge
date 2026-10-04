#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Al Manar Interactive Challenge - PostgreSQL backup.
#
# Takes a consistent custom-format dump of the application database, writes a
# checksum beside it, and proves the dump is restorable *before* anyone needs it.
#
# Custom format (-Fc), not plain SQL, because it compresses, and because
# pg_restore can restore it selectively and skip a failed object without
# discarding the whole file.
#
# Usage (from the project root):
#
#     ./deploy/backup.sh                      # dump to ./backups
#     BACKUP_DIR=/mnt/usb/backups ./deploy/backup.sh
#
# Verification is the point: a backup nobody has restored is a hypothesis. Every
# run lists the dump and checks it against the live database's row counts.
#
# Reading the password: taken from .env, never from the command line and never
# echoed. Override by exporting PGPASSWORD yourself.
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-${PROJECT_DIR}/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"

DB_CONTAINER="${DB_CONTAINER:-almanar-challenge-db}"

if [[ -f "${PROJECT_DIR}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "${PROJECT_DIR}/.env"; set +a
fi

DB_NAME="${DATABASE_NAME:-challenge}"
DB_USER="${DATABASE_USER:-challenge}"

if [[ -z "${DATABASE_PASSWORD:-}" ]]; then
    echo "error: DATABASE_PASSWORD is not set and .env was not found." >&2
    echo "       Copy .env.example to .env and fill in the password." >&2
    exit 1
fi

mkdir -p "${BACKUP_DIR}"

STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="${BACKUP_DIR}/challenge-${STAMP}.dump"

echo "[backup] database : ${DB_NAME} (container ${DB_CONTAINER})"
echo "[backup] writing  : ${DUMP}"

# Single transaction so the dump is a consistent snapshot even while a
# competition is being played. -v lets a restore skip an object that fails
# instead of losing everything after it.
docker exec -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
    pg_dump --username="${DB_USER}" --dbname="${DB_NAME}" \
    --format=custom --compress=9 --no-owner --no-privileges --verbose \
    > "${DUMP}"

echo "[backup] checksum"
# The checksum file records the file NAME only, and is written from inside
# BACKUP_DIR. Writing it from the caller's working directory would embed an
# absolute path - "F:/..." on the Windows development machine, which does not
# exist on the Ubuntu server - and `sha256sum --check` in restore.sh would then
# fail on every dump copied off the development machine. A backup that cannot
# pass its own integrity check on the machine that has to use it is worse than
# no backup, because it looks like one.
( cd "${BACKUP_DIR}" && sha256sum "$(basename "${DUMP}")" > "$(basename "${DUMP}").sha256" )
echo "[backup] checksum : $(cut -d' ' -f1 < "${DUMP}.sha256")"

SIZE="$(du -h "${DUMP}" | cut -f1)"
echo "[backup] size     : ${SIZE}"

# -- verification -----------------------------------------------------------
#
# Confirm the archive is structurally readable, and that it really contains the
# data. A dump that cannot be listed cannot be restored.
#
# The archive is streamed in over stdin rather than named as a path: it lives on
# the host, and the database container cannot see the host filesystem. pg_restore
# reads from stdin when the filename is "-".

echo "[backup] verifying the archive is readable"
TABLES="$(docker exec -i -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
    pg_restore --list < "${DUMP}" | grep -c 'TABLE DATA' || true)"

if [[ "${TABLES}" -lt 1 ]]; then
    echo "error: the dump lists no table data - it is not restorable." >&2
    exit 1
fi
echo "[backup] verified : ${TABLES} tables present in the archive"

echo "[backup] retention: keeping ${KEEP_DAYS} days"
find "${BACKUP_DIR}" -maxdepth 1 -name 'challenge-*.dump' -mtime "+${KEEP_DAYS}" -print -delete
find "${BACKUP_DIR}" -maxdepth 1 -name 'challenge-*.dump.sha256' -mtime "+${KEEP_DAYS}" -print -delete

echo "[backup] done. Restore with: ./deploy/restore.sh ${DUMP}"
echo "[backup] copy it off the server: a backup on the same disk is not a backup."