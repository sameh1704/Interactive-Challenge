#!/bin/sh
# Al Manar Interactive Challenge - canonical production deployment.
#
# Usage:
#   ./deploy-production.sh            # build, prepare, recreate the application
#   ./deploy-production.sh --no-build # prepare and use the existing image
#
# Why this script exists: running `docker compose up -d --build` with only the
# base file (docker-compose.yml) starts challenge-web with development defaults
# and, critically, WITHOUT the production overlay's environment passthrough.
# The running container then has no SESSION_COOKIE_SECURE / CSRF_COOKIE_SECURE
# variables at all, so production.py falls back to its compiled default of
# True; on this plain-HTTP deployment the browser drops the Secure cookies and
# every login fails with 403 "CSRF verification failed" while /health/ stays
# green. The only supported production command is the one below, with BOTH
# files.
set -eu

case "${1:-}" in
    ""|--no-build) ;;
    *) echo "Usage: $0 [--no-build]" >&2; exit 2 ;;
esac

cd "$(dirname "$0")"

compose() {
    docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"
}

compose config --quiet
if [ "${1:-}" != "--no-build" ]; then
    compose build challenge-web
fi
# Datastore services must already be running. Fail visibly if preparation fails;
# never fall back to restarting the entire stack.
compose run --rm --no-deps challenge-prepare
compose up -d --no-deps challenge-web

echo "[deploy] waiting for a healthy web container..."
READY=false
for _ in $(seq 1 24); do
    if [ "$(docker inspect --format '{{.State.Health.Status}}' almanar-challenge-web)" = "healthy" ]; then
        READY=true
        break
    fi
    sleep 5
done
if [ "$READY" != true ]; then
    echo "[deploy] web container did not become healthy" >&2
    exit 1
fi

echo "[deploy] deployment checks:"
docker exec almanar-challenge-web python manage.py check --deploy
echo "[deploy] login-page smoke test:"
curl -sI --max-time 10 http://127.0.0.1/accounts/login/ | head -8
