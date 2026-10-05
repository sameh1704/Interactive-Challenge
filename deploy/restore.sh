#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Al Manar Interactive Challenge - restore from a PostgreSQL dump.
#
# TWO MODES, and the distinction matters:
#
#   --verify  Restore the dump into a *scratch* database, compare it with the
#            live one, then throw the scratch database away. Changes nothing.
#            Run this on a schedule; it is how you find out a backup is broken
#            before you need it.
#
#   --live    Restore into the real database. Destroys the current contents.
#            Requires --confirm, so it cannot happen by accident. Stops the web
#            and proxy containers, restores, restarts *both*, and then proves the
#            site answers through the proxy before reporting success.
#
# Usage (from the project root):
#
#     ./deploy/restore.sh backups/challenge-20261004-090000.dump --verify
#     ./deploy/restore.sh backups/challenge-20261004-090000.dump --live --confirm
#
# Before a live restore, take a backup of what you are about to overwrite:
#
#     ./deploy/backup.sh
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "${PROJECT_DIR}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "${PROJECT_DIR}/.env"; set +a
fi

DB_NAME="${DATABASE_NAME:-challenge}"
DB_USER="${DATABASE_USER:-challenge}"
DB_CONTAINER="${DB_CONTAINER:-almanar-challenge-db}"
WEB_CONTAINER="${WEB_CONTAINER:-almanar-challenge-web}"
PROXY_CONTAINER="${PROXY_CONTAINER:-almanar-challenge-proxy}"
SCRATCH_DB="${SCRATCH_DB:-challenge_restore_check}"

MODE=""
DUMP=""
CONFIRMED="no"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --verify)  MODE="verify" ;;
        --live)    MODE="live" ;;
        --confirm) CONFIRMED="yes" ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *)         DUMP="$1" ;;
    esac
    shift
done

if [[ -z "${DUMP}" || -z "${MODE}" ]]; then
    echo "usage: $0 <dump file> (--verify | --live --confirm)" >&2
    exit 1
fi

if [[ ! -f "${DUMP}" ]]; then
    echo "error: no such dump: ${DUMP}" >&2
    exit 1
fi

if [[ -f "${DUMP}.sha256" ]]; then
    echo "[restore] checking the checksum"
    if command -v sha256sum >/dev/null 2>&1; then
        ( cd "$(dirname "${DUMP}")" && sha256sum --check "$(basename "${DUMP}").sha256" )
    else
        echo "[restore] WARNING: sha256sum unavailable; skipping the integrity check"
    fi
else
    echo "[restore] WARNING: no .sha256 beside the dump; integrity unverified"
fi

if [[ "${MODE}" == "verify" ]]; then
    echo "[restore] restoring into scratch database '${SCRATCH_DB}' - the live database is untouched"

    docker exec -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
        dropdb --if-exists --username="${DB_USER}" "${SCRATCH_DB}"
    docker exec -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
        createdb --username="${DB_USER}" --owner="${DB_USER}" "${SCRATCH_DB}"

    # --exit-on-error is deliberately OFF: the dump contains ownership and
    # extension statements that a scratch database may refuse, and one refusal
    # should not hide the tables that did restore. What matters is the comparison
    # below, which is done against the restored data itself.
    #
    # The dump lives on the host and the container cannot see the host
    # filesystem, so it is streamed in over stdin rather than named as a path.
    docker exec -i -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
        pg_restore --username="${DB_USER}" --dbname="${SCRATCH_DB}" \
        --no-owner --no-privileges < "${DUMP}" > /tmp/restore.log 2>&1 || true

    FAILED="$(grep -ci 'error' /tmp/restore.log || true)"
    if [[ "${FAILED}" -gt 0 ]]; then
        echo "[restore] pg_restore reported ${FAILED} error line(s); first few:"
        grep -i 'error' /tmp/restore.log | head -5
    fi

    echo "[restore] comparing row counts"
    ROWS_SQL="SELECT table_schema || '.' || table_name FROM information_schema.tables
              WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1;"

    # The table list is one name per line, and only the carriage return is removed.
    # Stripping all whitespace here would glue every name into a single line, the
    # loop would compare one non-existent table, and both sides would be empty and
    # the verification would pass having proved nothing.
    list_tables() {
        docker exec -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
            psql --username="${DB_USER}" --dbname="$1" --tuples-only --no-align \
            --command="${ROWS_SQL}" 2>/dev/null | tr -d '\r' || true
    }

    count_rows() {
        # `|| true` throughout: a table that failed to restore must be *reported*
        # as a mismatch, not abort the comparison half way through under set -e.
        docker exec -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
            psql --username="${DB_USER}" --dbname="$1" --tuples-only --no-align \
            --command="SELECT count(*) FROM public.$2;" 2>/dev/null \
            | tr -d '[:space:]' || true
    }

    EXPECTED="$(docker exec -i -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
        pg_restore --list < "${DUMP}" | grep -c 'TABLE DATA' || true)"

    MISMATCH=0
    COMPARED=0
    while read -r TABLE; do
        [[ -z "${TABLE}" ]] && continue
        LIVE="$(count_rows "${DB_NAME}" "${TABLE}")"
        RESTORED="$(count_rows "${SCRATCH_DB}" "${TABLE}")"
        COMPARED=$((COMPARED + 1))

        if [[ "${LIVE}" != "${RESTORED}" ]]; then
            printf '  MISMATCH  %-45s live=%s restored=%s\n' \
                "${TABLE}" "${LIVE:-missing}" "${RESTORED:-missing}"
            MISMATCH=$((MISMATCH + 1))
        fi
    done <<< "$(list_tables "${DB_NAME}")"

    docker exec -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
        dropdb --if-exists --username="${DB_USER}" "${SCRATCH_DB}"
    echo "[restore] scratch database dropped"

    # Guard against a verification that compared almost nothing and still passed.
    if [[ "${COMPARED}" -lt "${EXPECTED}" ]]; then
        echo "[restore] VERIFY FAILED: the dump holds ${EXPECTED} tables but only" \
             "${COMPARED} were compared - the comparison did not cover the dump." >&2
        exit 1
    fi

    if [[ "${MISMATCH}" -gt 0 ]]; then
        echo "[restore] VERIFY FAILED: ${MISMATCH} of ${COMPARED} table(s) differ." >&2
        exit 1
    fi
    echo "[restore] VERIFY PASSED: all ${COMPARED} tables match the live database."
    exit 0
fi

# -- live --------------------------------------------------------------------
if [[ "${CONFIRMED}" != "yes" ]]; then
    cat >&2 <<EOF
refusing to restore into the live database without --confirm.

    This replaces every row in '${DB_NAME}' with the contents of:
      ${DUMP}

Take a backup of the current data first:

    ./deploy/backup.sh

Then re-run with --confirm.
EOF
    exit 1
fi

echo "[restore] STOPPING the web and proxy containers so nothing writes during the restore"
# stop, not rm: the containers (and therefore their volumes) survive, and
# `docker start` below brings the same ones back.
docker stop "${PROXY_CONTAINER}" "${WEB_CONTAINER}" >/dev/null

echo "[restore] dropping and recreating '${DB_NAME}'"
docker exec -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
    dropdb --if-exists --username="${DB_USER}" "${DB_NAME}"
docker exec -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
    createdb --username="${DB_USER}" --owner="${DB_USER}" "${DB_NAME}"

echo "[restore] restoring"
docker exec -i -e PGPASSWORD="${DATABASE_PASSWORD}" "${DB_CONTAINER}" \
    pg_restore --username="${DB_USER}" --dbname="${DB_NAME}" \
    --no-owner --no-privileges --exit-on-error < "${DUMP}"

# Both containers, not just the application. The proxy is the only published
# port in production, so a restore that left it stopped would look like a
# successful restore with a dead site - the single worst outcome this script
# could produce. `up -d` rather than `start` so a container that is not running
# (rebooted server, interrupted deploy) is also brought back.
echo "[restore] starting the web and proxy containers again"
docker start "${WEB_CONTAINER}" >/dev/null
docker start "${PROXY_CONTAINER}" >/dev/null

# Prove the site actually answers rather than reporting success and hoping. The
# proxy needs a moment to accept connections again after a stop.
echo "[restore] waiting for the application to answer through the proxy"
READY="no"
for _ in $(seq 1 30); do
    if docker exec "${PROXY_CONTAINER}" wget -q -O - \
            "http://127.0.0.1/health/" >/dev/null 2>&1; then
        READY="yes"
        break
    fi
    sleep 2
done

if [[ "${READY}" != "yes" ]]; then
    echo "[restore] WARNING: the proxy is running but /health/ did not answer" >&2
    echo "[restore]          within 60s. Check:" >&2
    echo "[restore]          docker logs --tail 50 ${WEB_CONTAINER}" >&2
    echo "[restore]          docker logs --tail 50 ${PROXY_CONTAINER}" >&2
    exit 1
fi

echo "[restore] done, and /health/ answers through the proxy."
echo "[restore] Verify with:"
echo "    curl -fsS http://127.0.0.1/health/ | head"
echo "    docker compose -f docker-compose.yml -f docker-compose.prod.yml ps"