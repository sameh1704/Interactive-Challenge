# Al Manar Interactive Challenge

A school-internal web application for running **live interactive educational
competitions** between classrooms, using the interactive screens and web
browsers that are already installed on the school network.

This repository is a **self-contained project**. It shares nothing with the
School Portal, Cisco NMS, any other Django project, or any other Docker Compose
project on the same machine.

---

## Current scope

**Phase 1 - Project Foundation. The competition engine is not built yet.**

Delivered:

| Capability | State |
| --- | --- |
| Django project on Docker Compose | Done |
| PostgreSQL as the only datastore | Done |
| Redis for cache and Django Channels | Done |
| Django Channels over ASGI (WebSocket ready) | Done |
| `GET /health/` with dependency checks | Done |
| `GET /` landing page | Done |
| Static files collected at build, served by WhiteNoise | Done |
| Media directory separate from static | Done |
| Automated test suite | Done |
| Environment-variable-only configuration | Done |
| Container health checks for all three services | Done |

Explicitly **not** built yet, by design: questions, competitions, scoring,
tournaments, teacher accounts, classroom logic, screen registration and the live
WebSocket engine.

---

## Architecture

```
                       ┌───────────────────────────────┐
   Browser / screen ──▶│  challenge-web                │
   (future phase)      │  Daphne (ASGI)                │
                       │    ├── HTTP   → Django        │
                       │    └── WS     → Channels      │
                       │                               │
                       │  Django 5.2 LTS              │
                       │  Channels 4.3  ──config──▶    │
                       │  WhiteNoise (static files)    │
                       └───────────────┬───────────────┘
                                       │  private network
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
        ┌───────────────────────────┐   ┌────────────────────────┐
        │  challenge-db             │   │  challenge-redis       │
        │  PostgreSQL 17            │   │  Redis 8               │
        │  challenge-db-data volume │   │  cache + channel layer │
        └───────────────────────────┘   │  appendonly persistence│
                                        └────────────────────────┘
```

* **One ASGI process** serves both HTTP and WebSocket traffic on port 8000, so
  a future live competition needs no extra realtime infrastructure.
* **PostgreSQL** holds every durable record. It is the only database backend
  the settings accept.
* **Redis** serves two distinct roles: the Django cache backend and the
  Channels layer transport (group/channel broadcast for live rounds).
* **Static files** are collected into the image at build time and served by
  WhiteNoise. **Media files** live in a separate Docker volume and are never
  served by Django in production — the front-end proxy handles them, so
  user-supplied files can never be interpreted as application code.

### Repository layout

```
almanar-interactive-challenge/
├── docker-compose.yml          # the whole stack, isolated from other projects
├── .env.example                # documented template for every variable
├── .gitignore
├── README.md
├── backend/
│   ├── Dockerfile              # python:3.13-slim, non-root, static collected
│   ├── requirements.txt        # fully pinned
│   ├── manage.py
│   ├── docker/
│   │   └── entrypoint.sh       # wait → system checks → migrate → exec server
│   ├── config/                 # project configuration, not a domain app
│   │   ├── asgi.py             # HTTP + WebSocket protocol router
│   │   ├── routing.py          # WebSocket URL table (empty in Phase 1)
│   │   ├── urls.py
│   │   ├── wsgi.py             # reference only; ASGI is the real runtime
│   │   ├── logging_config.py   # container-friendly structured logging
│   │   └── settings/
│   │       ├── base.py         # everything, all read from the environment
│   │       ├── development.py  # DEBUG conveniences
│   │       └── production.py   # hardened, DEBUG forced off
│   └── core/                   # landing page, health checks, ops commands
│       ├── health.py
│       ├── views.py
│       ├── urls.py
│       ├── management/commands/wait_for_services.py
│       ├── static/core/css/site.css
│       ├── templates/core/
│       └── tests/              # automated test suite
└── tests/
    ├── scripts/verify.ps1      # end-to-end verification runner
    └── phases/phase-01/        # acceptance record + manual procedure
```

### Why the Django tests are not in `tests/`

`tests/` at the project root holds the *verification artifacts* — the executable
verification runner and the per-phase acceptance record. The Django test suite
lives in `backend/core/tests/`, next to the code it tests, because that is where
Django's test runner discovers it. `tests/README.md` explains the split.

Future domain applications (`accounts`, `classrooms`, `screens`, `questions`,
`competitions`, `live`, `scoring`, `tournaments`, `reports`) are added as
separate Django apps in `backend/`. Only `core` and `config` exist today.

---

## Docker services

| Compose service | Container name | Image | Published to host |
| --- | --- | --- | --- |
| `challenge-web` | `almanar-challenge-web` | built from `backend/Dockerfile` | `${WEB_BIND_ADDRESS}:${WEB_PORT}` → 8000 |
| `challenge-db` | `almanar-challenge-db` | `postgres:17-alpine` | no (private network only) |
| `challenge-redis` | `almanar-challenge-redis` | `redis:8-alpine` | no (private network only) |

Isolation from every other project on the machine:

* Compose project name: `almanar-interactive-challenge`
* Network: `almanar-interactive-challenge-net` (dedicated bridge)
* Named volumes: `almanar-interactive-challenge_challenge-db-data`,
  `..._challenge-redis-data`, `..._challenge-media`
* All service, container, network and image names are prefixed

Port 5432 and 6379 are deliberately **not** published. Only the web port is
exposed, because only browsers need it.

---

## Environment variables

No secret, credential, key or fixed address is stored in this repository.
Everything comes from the environment. Copy `.env.example` to `.env` and fill
it in — `.env` is git-ignored.

### Application

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DJANGO_SECRET_KEY` | **yes** | — | Django signing key. Production refuses to start without it. |
| `DJANGO_ENV` | no | `development` | `development` or `production` |
| `DJANGO_SETTINGS_MODULE_NAME` | no | `development` | Selects `config.settings.<name>` |
| `DEBUG` | no | `True` in dev, forced `False` in production | Django debug mode |
| `ALLOWED_HOSTS` | **yes** in production | `localhost,127.0.0.1,[::1]` | Comma-separated; never `*` |
| `CSRF_TRUSTED_ORIGINS` | no | empty | Only for a proxy on another origin |
| `APP_VERSION` | no | `0.1.0` | Reported by `/health/` and the footer |
| `TIME_ZONE` | no | `UTC` | IANA time zone |
| `LOG_LEVEL` | no | `INFO` | Root logger level |
| `WEB_BIND_ADDRESS` | no | `0.0.0.0` | Host interface to publish |
| `WEB_PORT` | no | `8000` | Host port |
| `SERVICE_WAIT_TIMEOUT` | no | `60` | Seconds to wait for dependencies at startup |

Generate a secret key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### Configuration validation

Settings **always import successfully**, so that commands which legitimately run
without a database or cache (`collectstatic`, `makemigrations`) work during the
image build. Missing deployment variables are caught instead by Django system
checks in `core/checks.py`:

```bash
docker compose exec challenge-web python manage.py check
```

This reports a missing variable, or one still set to a placeholder
(`changeme`, `todo`, ...), as `challenge.E001` / `challenge.E002`. The container
entrypoint runs this check before serving traffic, so a misconfigured deployment
fails immediately rather than on the first request.

### PostgreSQL

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DATABASE_NAME` | **yes** | — | Database name |
| `DATABASE_USER` | **yes** | — | Database role |
| `DATABASE_PASSWORD` | **yes** | — | Database password |
| `DATABASE_HOST` | **yes** | `challenge-db` in Compose | Database host |
| `DATABASE_PORT` | **yes** | `5432` | Database port |
| `DATABASE_TEST_NAME` | no | `test_<DATABASE_NAME>` | Explicit test database |
| `DATABASE_CONN_MAX_AGE` | no | `60` | Persistent connection lifetime |
| `DATABASE_CONNECT_TIMEOUT` | no | `10` | Connect timeout in seconds |

`DATABASE_*` has a single source of truth: Docker Compose passes the same
values to the PostgreSQL container (`POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`) and to Django. Change them **before the first start**.

### Redis

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `REDIS_HOST` | **yes** | `challenge-redis` in Compose | Redis host |
| `REDIS_PORT` | **yes** | `6379` | Redis port |
| `REDIS_DB` | no | `0` | Logical database number |
| `REDIS_PASSWORD` | no | empty | Only if Redis authentication is enabled |
| `CACHE_KEY_PREFIX` | no | `almanar-challenge` | Namespaces cache keys |
| `CACHE_TIMEOUT` | no | `300` | Default cache TTL in seconds |
| `CHANNEL_LAYER_CAPACITY` | no | `1500` | Channels layer channel capacity |
| `CHANNEL_LAYER_EXPIRY` | no | `10` | Channel expiry in seconds |

### Production hardening

`SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
`SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD`.
All default to a safe-but-inert state; enable them only once HTTPS is verified
on the target server.

---

## How to start

### 1. Configure

```bash
cd almanar-interactive-challenge
cp .env.example .env
```

Then in `.env`:

1. set `DJANGO_SECRET_KEY` to a generated value,
2. set `DATABASE_PASSWORD` to a strong password,
3. set `ALLOWED_HOSTS` to the hostnames or IP addresses browsers will use.

### 2. Validate and start

```bash
docker compose config
docker compose up -d --build
docker compose ps
```

`docker compose ps` should list all three services as `healthy`. `challenge-web`
waits for both dependencies to report healthy before it even starts.

> **Host port.** `WEB_PORT` in `.env` controls which host port is published. Do
> not assume `8000` is free: another project or process on the same machine may
> already hold it, and Docker will refuse to start. If `docker compose up` fails
> with `ports are not available`, change `WEB_PORT` to a free port.

### 3. Verify

```bash
# Landing page and health endpoint
curl -i http://127.0.0.1:8000/
curl -s http://127.0.0.1:8000/health/

# Logs
docker compose logs -f challenge-web
```

`/health/` responds with JSON and returns **503** if PostgreSQL or Redis is
unavailable, which makes it usable directly as an uptime check.

### 4. Stop

```bash
docker compose down          # keep data
docker compose down -v       # delete database, Redis and media volumes
```

---

## How to run the tests

The suite needs real PostgreSQL and Redis, so run it inside the stack:

```bash
docker compose exec challenge-web python manage.py test --verbosity 2
```

Or run the full verification, including endpoint checks and dependency
connectivity:

```bash
pwsh -File tests/scripts/verify.ps1
```

Test modules:

| Module | Covers |
| --- | --- |
| `core/tests/test_configuration.py` | Django starts, no hard-coded secrets or addresses, static/media separation |
| `core/tests/test_landing.py` | `GET /` returns 200 and shows the project name |
| `core/tests/test_health.py` | `GET /health/` returns 200/503 correctly and leaks no details |
| `core/tests/test_database.py` | PostgreSQL is the backend, credentials come from the environment, the connection works |
| `core/tests/test_redis.py` | Cache and Channels layer reach real Redis, including a group send/receive round trip |
| `core/tests/test_checks.py` | Deployment system checks detect missing/placeholder variables |
| `core/tests/test_wait_for_services.py` | Entrypoint wait logic, plus the real checks against the live services |

Tests run against a separate `test_<DATABASE_NAME>` database and are wrapped in
transactions, so the live database is never touched. `test_redis.py` skips its
connectivity cases when Redis is unreachable instead of failing misleadingly.

---

## Design decisions that must survive later phases

* **Screen identity is a registered Screen ID, never an IP address.** IP
  addresses may be recorded as network/monitoring data, but a DHCP change must
  not change which screen is which. Phase 1 commits to this so that no early
  schema choice quietly reintroduces IP-based identity.
* **One ASGI server on one port.** WebSocket and HTTP must share a port so that
  a classroom screen only ever needs a single URL.
* **Environment-only configuration.** The application must run on any Linux
  server on the school network without editing source files.
* **No premature features.** Domain apps are created in the phase that needs
  them, so that each phase stays reviewable.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `challenge-web` never becomes healthy | Run `docker compose logs challenge-web`. Usually `wait_for_services` timing out because `challenge-db` or `challenge-redis` is unhealthy. |
| `ports are not available` on start | Another process already holds `WEB_PORT`. Pick a free port in `.env`. |
| `/health/` returns 503 | The JSON `checks` object names the failing dependency. Check `docker compose ps` and the relevant service logs. |
| `System check identified some issues` with `challenge.E001` | A required environment variable is missing. Run `manage.py check` inside the container for the exact name. |
| `DisallowedHost` | Add the hostname or IP address to `ALLOWED_HOSTS` in `.env`, then `docker compose up -d`. |
| Static files return 404 | The image collected static files at build time. Rebuild: `docker compose build --no-cache challenge-web`. |
| `SECRET_KEY` error on startup in production | Generate a key and set `DJANGO_SECRET_KEY` in `.env`. |
| `challenge.E003: ALLOWED_HOSTS contains a wildcard` | Replace `*` with the explicit hostnames or IP addresses. |
| Another project cannot see these containers | By design. Nothing outside `almanar-interactive-challenge-net` can reach the stack. |

---

## Licence

Internal school project. All rights reserved.

