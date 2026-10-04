# Phase 1 - Project Foundation: Acceptance Record

Date: 2026-10-01
Environment: Windows 11, PowerShell 7, Docker Engine 29.6.2, Docker Compose v5.3.1

## Acceptance criteria

| # | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Docker Compose starts successfully | **PASS** | `docker compose up -d --build` exit 0; cold start after `docker compose down` also exit 0 |
| 2 | PostgreSQL is healthy | **PASS** | `almanar-challenge-db ... Up (healthy)`; `pg_isready` → `accepting connections`; PostgreSQL 17.11 |
| 3 | Redis is healthy | **PASS** | `almanar-challenge-redis ... Up (healthy)`; `redis-cli ping` → `PONG`; Redis 8.10.2 |
| 4 | Django is healthy | **PASS** | `almanar-challenge-web ... Up (healthy)` |
| 5 | `/health/` works | **PASS** | HTTP 200 with `{"status":"ok"}`; verified HTTP 503 when Redis is stopped |
| 6 | `/` works | **PASS** | HTTP 200, 1486 bytes, renders "Al Manar" / "Interactive Challenge" |
| 7 | Automated tests pass | **PASS** | 66 tests, `OK`, 0 failures, 0 errors, 0 skips |
| 8 | Existing projects were not touched | **PASS** | `frigate`, `frigate-mosquitto`, `n8n` still `Up` and healthy; no `docker` command was issued against their compose files |
| 9 | No hard-coded secrets exist | **PASS** | Enforced by `test_configuration.py` and `test_checks.py`; `.env` git-ignored and holds generated values |
| 10 | README is present | **PASS** | `README.md` at the project root |

## Automated test suite

Command:

```bash
docker compose exec -T challenge-web python manage.py test --verbosity 2
```

Result:

```
Ran 66 tests in 0.326s

OK
```

No test was skipped: the Redis connectivity cases executed against the live
Redis container.

### Breakdown by module

| Module | Tests | Covers |
| --- | --- | --- |
| `core/tests/test_configuration.py` | 15 | Django starts; environment-only configuration; no literal credentials or private addresses in source; static/media separation; WhiteNoise |
| `core/tests/test_landing.py` | 6 | `GET /` returns 200 and shows the project name |
| `core/tests/test_health.py` | 7 | `GET /health/` returns 200/503 correctly and leaks no internals |
| `core/tests/test_database.py` | 7 | PostgreSQL is the backend, credentials come from the environment, the connection works, tests use a separate database |
| `core/tests/test_redis.py` | 12 | Cache backend, Channels layer configuration, and a real group send/receive round trip over Redis |
| `core/tests/test_checks.py` | 9 | Deployment system checks detect missing/placeholder variables and a wildcard host |
| `core/tests/test_wait_for_services.py` | 6 | Entrypoint wait logic, plus the real checks against the live services |

## End-to-end verification script

Command:

```bash
pwsh -File tests/scripts/verify.ps1 -SkipBuild
```

Result: `VERIFICATION PASSED: every check succeeded.` (21 assertions, exit 0)

## Defects found during Phase 1 and how they were fixed

These were found by actually running and testing, not by inspection.

### 1. `channels_redis` was never installed (functional defect)

`channels[daphne]` does **not** pull in `channels-redis`; it is a separate
distribution. The Channels layer raised
`InvalidChannelLayerError: Cannot import BACKEND 'channels_redis.core.RedisChannelLayer'`.

This would have broken the first WebSocket connection in a later phase, while
the HTTP pages looked perfectly healthy.

Fix: added `channels-redis==4.3.0` (and its `msgpack` dependency) to
`requirements.txt`, plus a regression test that resolves the channel layer and
performs a real group send/receive round trip.

### 2. Settings raised at import time, breaking `collectstatic` (design defect)

`config/settings/base.py` raised `ImproperlyConfigured` when `DATABASE_NAME`,
`DATABASE_USER` or `REDIS_HOST` were absent. Those variables only exist at
runtime, so `python manage.py collectstatic` during the image build could never
load settings and failed with a misleading `KeyError: 'collectstatic'`.

Fix: the settings module now always imports; required-variable validation moved
into Django system checks in `core/checks.py`, which the container entrypoint
runs via `manage.py check` before serving traffic. Behaviour is now stricter
and better reported, not weaker.

### 3. Static storage backend was chosen from `DEBUG` (latent defect)

`STORAGES` was built in `base.py` from `DEBUG`, but `development.py` overrides
`DEBUG` only *after* `base.py` has been evaluated, so the two modules could
disagree about the storage backend.

Fix: the backend is now selected from `DJANGO_ENV`, which is known before the
storage dictionary is built.

### 4. Verification script assumed port 8000 (tooling defect)

`tests/scripts/verify.ps1` hardcoded `http://127.0.0.1:8000`. Port 8000 is
occupied on this machine by an unrelated local `python` process, so the script
silently queried that process: `GET /` returned its 200 page and `/health/`
returned 404.

Fix: the script reads `WEB_PORT` from `.env` and prints the target it will use.
This is the same "never assume a fixed address" rule the project applies to the
application itself.

### 5. Four test-design defects

* `test_real_checks_pass_against_the_docker_services` was a `SimpleTestCase`,
  which forbids database queries, so the database health check could never
  succeed. Moved to `TestCase`.
* `test_credentials_come_from_the_environment` compared `NAME` against
  `DATABASE_NAME`, but Django's test runner rewrites it to the test database.
  `NAME` is now asserted separately.
* `test_a_missing_variable_is_reported` used `mock.patch.dict` expecting it to
  delete a key; it only sets keys. The variable is now popped explicitly.
* `test_checks_are_registered_with_django` used `get_checks()` without
  `include_deployment_checks=True`, so the `deploy=True` check appeared
  missing.
* `test_channel_layer_uses_the_configured_redis_host` called
  `groups_for`, which Django Channels 4 removed. Replaced with a real group
  send/receive round trip.
* `test_a_failed_statement_raises_and_keeps_the_session_usable` asserted
  incorrect behaviour: PostgreSQL aborts the entire transaction on error, and
  `TestCase` wraps every test in one. The test now asserts the correct
  guarantee, recovery through the surrounding savepoint.

## Isolation evidence

```
$ docker ps --format '{{.Names}}\t{{.Status}}'
almanar-challenge-db       Up (healthy)
almanar-challenge-redis    Up (healthy)
almanar-challenge-web      Up (healthy)
frigate                    Up 27 minutes (healthy)
frigate-mosquitto          Up 27 minutes
n8n                        Up 27 minutes

$ docker compose ls
Name                          Status      ConfigFiles
almanar-interactive-challenge running(3) ...\almanar-interactive-challenge\docker-compose.yml
frigate                       running(2) F:\frigate\docker-compose.yml
```

Resources created by this project, all distinctly named:

```
almanar-challenge-web:0.1.0                                       (image)
almanar-interactive-challenge_challenge-db-data                   (volume)
almanar-interactive-challenge_challenge-media                     (volume)
almanar-interactive-challenge_challenge-redis-data                (volume)
almanar-interactive-challenge-net                                 (network)
```