#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Al Manar Interactive Challenge - verify that the newest backup still restores.
#
# A backup nobody has restored is a hypothesis. This is the scheduled half of
# `restore.sh --verify`: it finds the most recent dump in the backup directory
# and restores it into a *scratch* database, compares every table against the
# live one, then drops the scratch database. The live database is never
# modified -- `--verify` stops the scratch database being dropped and the real
# one being written to before anything happens.
#
# Wired to the weekly timer installed by `deploy/install-backup-timer.sh`. Safe
# to run by hand at any time; exits non-zero if the newest dump is unusable, so
# the timer records the failure rather than reporting a healthy backup week.
#
# Reads BACKUP_DIR, KEEP_DAYS and DB_CONTAINER from the environment file the
# installer wrote, falling back to the same defaults, so it and backup.sh always
# agree on where dumps live.
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=almanar-challenge-backup
DB_CONTAINER="${DB_CONTAINER:-almanar-challenge-db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/almanar-interactive-challenge}"

# Same environment file the timer uses, so a run from the timer and a run by
# hand resolve the same directory. Absent when run before the timer is installed,
# which is fine - the defaults above then apply.
if [[ "$(id -u)" -eq 0 && -f "/etc/${UNIT}.conf" ]]; then
    # shellcheck disable=SC1091
    set -a; source "/etc/${UNIT}.conf"; set +a
elif [[ -f "${HOME}/.config/almanar-challenge/${UNIT}.conf" ]]; then
    # shellcheck disable=SC1091
    set -a; source "${HOME}/.config/almanar-challenge/${UNIT}.conf"; set +a
fi

echo "[verify-backup] looking for the newest dump in ${BACKUP_DIR}"

if [[ ! -d "${BACKUP_DIR}" ]]; then
    echo "[verify-backup] FAIL: ${BACKUP_DIR} does not exist." >&2
    echo "[verify-backup] Is the backup timer installed and has it ever run?" >&2
    exit 1
fi

# `-t` newest first. Deliberately not a glob: with no matches the shell would
# leave the literal pattern and this would try to verify a file named `*.dump`,
# which reports a confusing "no such file" instead of "there are no backups".
LATEST="$(find "${BACKUP_DIR}" -maxdepth 1 -name 'challenge-*.dump' -type f \
    -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2- || true)"

if [[ -z "${LATEST}" ]]; then
    echo "[verify-backup] FAIL: no challenge-*.dump in ${BACKUP_DIR}." >&2
    echo "[verify-backup] There is nothing to restore. Check ${UNIT}.service." >&2
    exit 1
fi

echo "[verify-backup] newest dump: ${LATEST}"
echo "[verify-backup] size:        $(du -h "${LATEST}" | cut -f1)"
echo "[verify-backup] age:         $(( ($(date +%s) - $(stat -c %Y "${LATEST}")) / 3600 )) hour(s)"

echo "[verify-backup] restoring into a scratch database (the live one is untouched)"
echo "[verify-backup] logs: ${PROJECT_DIR}/docs/BACKUP-RESTORE.md"

DB_CONTAINER="${DB_CONTAINER}" "${PROJECT_DIR}/deploy/restore.sh" "${LATEST}" --verify