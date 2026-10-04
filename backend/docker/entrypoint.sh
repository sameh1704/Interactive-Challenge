#!/bin/sh
# Container entrypoint.
#
# 1. Wait until PostgreSQL and Redis accept connections.
# 2. Validate the deployment configuration (system checks).
# 3. Apply database migrations.
# 4. Hand over to the configured server via exec, so that the server becomes
#    PID 1 and receives SIGTERM for a clean container shutdown.
set -eu

echo "[entrypoint] starting: ${APP_NAME:-almanar-interactive-challenge} (${DJANGO_ENV:-development})"

python manage.py wait_for_services --timeout "${SERVICE_WAIT_TIMEOUT:-60}"

# Validate the deployment configuration. The settings module always imports, so
# this is where a missing or placeholder environment variable is caught.
python manage.py check

python manage.py migrate --noinput

echo "[entrypoint] launching: $*"

exec "$@"