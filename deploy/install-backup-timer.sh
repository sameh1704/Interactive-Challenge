#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Al Manar Interactive Challenge - install the scheduled backup.
#
# Installs a systemd timer that runs `deploy/backup.sh` once a day and verifies
# the most recent dump once a week. Two variants, chosen automatically:
#
#   root        -> system units in /etc/systemd/system, backups in
#                  /var/backups/almanar-interactive-challenge. This is the
#                  arrangement to use on the school server.
#   not root    -> user units in ~/.config/systemd/user, backups in
#                  /opt/almanar/backups/almanar-interactive-challenge. Needs
#                  `loginctl enable-linger` so the timer survives logout and
#                  reboots; the script does that for you if it can.
#
# The user variant exists because a backup that needs root to schedule is a
# backup nobody sets up before they need it. Both call the same backup script and
# write to the same kind of place; only the privilege level differs.
#
# Usage:
#     ./deploy/install-backup-timer.sh            # install and enable
#     ./deploy/install-backup-timer.sh --uninstall
#     ./deploy/install-backup-timer.sh --status
#
# Re-running is safe: the units are overwritten in place and then re-enabled.
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

UNIT=almanar-challenge-backup
VERIFY_UNIT=almanar-challenge-backup-verify
SCHEDULE="${BACKUP_SCHEDULE:-*-*-* 02:30:00}"
VERIFY_SCHEDULE="${BACKUP_VERIFY_SCHEDULE:-Sun *-*-* 03:15:00}"

# 30 days, as required. Kept in the environment file rather than hard-coded in
# the unit so an administrator can change retention without editing a unit file.
KEEP_DAYS="${KEEP_DAYS:-30}"

MODE="${1:---install}"
JOURNAL=""

if [[ "$(id -u)" -eq 0 ]]; then
    BACKUP_DIR="${BACKUP_DIR:-/var/backups/almanar-interactive-challenge}"
    UNIT_DIR=/etc/systemd/system
    ENV_DIR=/etc
    systemctl_action() { systemctl "$@"; }
else
    BACKUP_DIR="${BACKUP_DIR:-/opt/almanar/backups/almanar-interactive-challenge}"
    UNIT_DIR="${HOME}/.config/systemd/user"
    ENV_DIR="${HOME}/.config/almanar-challenge"
    # The user journal, and the user timers target. Both differ from the system
    # equivalents, which is why they are variables rather than literals.
    JOURNAL="--user"
    systemctl_action() { systemctl --user "$@"; }
fi

ENV_FILE="${ENV_DIR}/${UNIT}.conf"
mkdir -p "$BACKUP_DIR" "$UNIT_DIR" "$ENV_DIR"

case "$MODE" in
--status)
    echo "[backup-timer] configuration"
    echo "    mode:        $([[ "$(id -u)" -eq 0 ]] && echo system || echo user)"
    echo "    backup dir:  ${BACKUP_DIR}"
    echo "    retention:   ${KEEP_DAYS} days"
    echo "    schedule:    ${SCHEDULE}"
    echo "    verify:      ${VERIFY_SCHEDULE}"
    echo "[backup-timer] timers"
    systemctl_action list-timers --all --no-pager | grep -E "${UNIT}|${VERIFY_UNIT}" \
        || echo "    not installed"
    exit 0
    ;;
--uninstall)
    for unit in "$UNIT.timer" "$UNIT.service" "$VERIFY_UNIT.timer" "$VERIFY_UNIT.service"; do
        systemctl_action disable --now "$unit" >/dev/null 2>&1 || true
        rm -f "${UNIT_DIR}/${unit}"
    done
    systemctl_action daemon-reload
    rm -f "$ENV_FILE"
    echo "[backup-timer] removed. Backups left in place at ${BACKUP_DIR}."
    exit 0
    ;;
--install) ;;
*)
    echo "usage: $0 [--install | --uninstall | --status]" >&2
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# Environment file: where backups go and how many are kept
# ---------------------------------------------------------------------------
#
# `deploy/backup.sh` reads these, so the schedule and a manual run behave
# identically. It already writes a sha256 beside every dump and prunes anything
# older than KEEP_DAYS, so retention and integrity are not reimplemented here.
umask 077
cat > "$ENV_FILE" <<EOF
# Al Manar Interactive Challenge - scheduled backup configuration.
# Written by deploy/install-backup-timer.sh. Edit and re-run that script, or
# edit this file and then: systemctl daemon-reload (or systemctl --user ...).
BACKUP_DIR=${BACKUP_DIR}
KEEP_DAYS=${KEEP_DAYS}
# The database container the dump is taken from.
DB_CONTAINER=${DB_CONTAINER:-almanar-challenge-db}
EOF
chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
#
# Written by this script rather than shipped as files so the absolute paths are
# always correct for the machine it is installed on, and so the unit that gets
# installed is the unit that was reviewed.
#
# `After=docker.service` because the dump is taken by exec'ing pg_dump inside
# the running database container; without it a boot-time timer could fire before
# Docker and fail.
write_service() {
    local path="$1" exec_start="$2" desc="$3"
    # `After=`/`Requires=` on docker.service only for a *system* install: a user
    # manager cannot see system units, and naming one there makes the start fail
    # with "Unit docker.service not found" rather than running the backup.
    local docker_dep=""
    if [[ "$(id -u)" -eq 0 ]]; then
        docker_dep=$'After=docker.service\nRequires=docker.service\n'
    fi
    cat > "$path" <<EOF
[Unit]
Description=${desc}
Documentation=file://${PROJECT_DIR}/docs/BACKUP-RESTORE.md
${docker_dep}
[Service]
Type=oneshot
EnvironmentFile=${ENV_FILE}
ExecStart=${exec_start}

# A failed backup must be visible, not quietly skipped. The unit exits non-zero
# if the dump fails or the archive turns out not to be restorable, which is what
# the timer records and \`systemctl status\` reports. Change OnFailure here to
# mail a school administrator once one is configured; there is deliberately no
# network destination baked in.
#
# Give a large database room to finish. This is a single dump, not an export of
# the application.
TimeoutStartSec=3600

# The script needs the Docker socket and nothing else.
NoNewPrivileges=true
PrivateTmp=true
EOF
}

write_timer() {
    local path="$1" schedule="$2" desc="$3"
    cat > "$path" <<EOF
[Unit]
Description=${desc}
Documentation=file://${PROJECT_DIR}/docs/BACKUP-RESTORE.md

[Timer]
OnCalendar=${schedule}

# If the server was off or the timer did not fire - a power cut, a long outage -
# run the missed backup as soon as it is back rather than waiting for the next
# scheduled slot. This is the difference between a backup that is merely old and
# one that is missing.
Persistent=true

# Spread the start a little so this server does not do a database dump at the
# same moment as everything else scheduled for the small hours.
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
EOF
}

write_service "${UNIT_DIR}/${UNIT}.service" \
    "/usr/bin/env bash ${PROJECT_DIR}/deploy/backup.sh" \
    "Al Manar Interactive Challenge - PostgreSQL backup"

write_timer "${UNIT_DIR}/${UNIT}.timer" "$SCHEDULE" \
    "Al Manar Interactive Challenge - daily backup"

write_service "${UNIT_DIR}/${VERIFY_UNIT}.service" \
    "/usr/bin/env bash ${PROJECT_DIR}/deploy/verify-latest-backup.sh" \
    "Al Manar Interactive Challenge - verify the most recent backup restores"

write_timer "${UNIT_DIR}/${VERIFY_UNIT}.timer" "$VERIFY_SCHEDULE" \
    "Al Manar Interactive Challenge - weekly restore verification"

systemctl_action daemon-reload
systemctl_action enable --now "${UNIT}.timer"
systemctl_action enable --now "${VERIFY_UNIT}.timer"

if [[ "$(id -u)" -ne 0 ]]; then
    # Without linger the user manager stops at logout, and so does the timer.
    if command -v loginctl >/dev/null 2>&1; then
        loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
        echo "[backup-timer] linger: $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)"
    fi
fi

echo "[backup-timer] installed"
echo "    mode:        $([[ "$(id -u)" -eq 0 ]] && echo system || echo user)"
echo "    backups:     ${BACKUP_DIR}"
echo "    retention:   ${KEEP_DAYS} days"
echo "    daily:       ${SCHEDULE}"
echo "    verify:      ${VERIFY_SCHEDULE}"
echo "    config:      ${ENV_FILE}"
echo
echo "[backup-timer] next runs"
systemctl_action list-timers --all --no-pager | grep -E "${UNIT}|${VERIFY_UNIT}" || true
echo
echo "[backup-timer] logs:    journalctl ${JOURNAL} -u ${UNIT}.service"
echo "[backup-timer] verify:   journalctl ${JOURNAL} -u ${VERIFY_UNIT}.service"
echo "[backup-timer] restore:  ./deploy/restore.sh ${BACKUP_DIR}/<dump> --verify"
echo "[backup-timer] to test now without waiting:"
echo "    systemctl ${JOURNAL} start ${UNIT}.service && journalctl ${JOURNAL} -u ${UNIT}.service -n 30"