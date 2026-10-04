# Phase 1 - Manual Test Procedure

Repeatable procedure for verifying the foundation by hand. Every command is run
from the project directory:

```bash
cd almanar-interactive-challenge
```

Throughout, `$PORT` is the value of `WEB_PORT` in `.env`.

---

## 1. Configuration

### 1.1 `.env` exists and holds real values

```bash
docker compose config --quiet && echo "compose file valid"
```

Expected: `compose file valid`, exit code 0.

### 1.2 The resolved configuration contains no placeholder secrets

```bash
docker compose config | grep -iE 'secret_key|password'
```

Expected: your own generated values, never `changeme` or an empty string.

---

## 2. Start the stack

```bash
docker compose up -d --build
docker compose ps
```

Expected: three services, each `Up (healthy)`.

| Service | Expected image | Expected status |
| --- | --- | --- |
| `challenge-db` | `postgres:17-alpine` | `Up (healthy)` |
| `challenge-redis` | `redis:8-alpine` | `Up (healthy)` |
| `challenge-web` | `almanar-challenge-web:0.1.0` | `Up (healthy)` |

`challenge-web` should only start **after** the other two report healthy.

### 2.1 Startup log

```bash
docker compose logs challenge-web | head -30
```

Expected sequence:

```
[entrypoint] starting: almanar-interactive-challenge (development)
All services are ready.
System check identified no issues (0 silenced).
Operations to perform:
  Apply all migrations: auth, contenttypes, sessions
  ...
[entrypoint] launching: daphne -b 0.0.0.0 -p 8000 config.asgi:application
```

---

## 3. HTTP endpoints

### 3.1 Landing page

Open `http://127.0.0.1:$PORT/` in a browser, or:

```bash
curl -i http://127.0.0.1:$PORT/
```

Expected: `HTTP/1.1 200 OK`, and the page shows:

* **AL MANAR**
* **INTERACTIVE CHALLENGE**
* a short project description

### 3.2 Stylesheet loads

From the landing page HTML, copy the `/static/...` href and request it.

Expected: `200 OK` and a `text/css` response. This proves WhiteNoise is serving
the files that were collected during the image build.

### 3.3 Health endpoint

```bash
curl -i http://127.0.0.1:$PORT/health/
```

Expected: `HTTP/1.1 200 OK` and JSON like:

```json
{
  "status": "ok",
  "service": "challenge-web",
  "version": "0.1.0",
  "environment": "development",
  "checks": {
    "database": {"status": "ok", "engine": "postgresql", "latency_ms": 17.21},
    "redis": {"status": "ok", "latency_ms": 7.08}
  },
  "timestamp": "..."
}
```

### 3.4 Health endpoint reports failure correctly

```bash
docker compose stop challenge-redis
curl -i http://127.0.0.1:$PORT/health/
docker compose start challenge-redis
```

Expected while Redis is down: `HTTP/1.1 503 Service Unavailable` and
`"redis": {"status": "unavailable"}`, with **no** host name, password or
traceback in the body.

Expected after restart: `200 OK` again, without restarting `challenge-web`.

---

## 4. Dependency connectivity

### 4.1 PostgreSQL

```bash
docker compose exec challenge-db pg_isready -U challenge -d challenge
docker compose exec challenge-db psql -U challenge -d challenge -c "SELECT version();"
docker compose exec challenge-db psql -U challenge -d challenge -c "\dt"
```

Expected: `accepting connections`, a PostgreSQL version string, and Django's
tables (`auth_*`, `django_migrations`, ...).

### 4.2 Redis

```bash
docker compose exec challenge-redis redis-cli ping
docker compose exec challenge-redis redis-cli info server | grep redis_version
```

Expected: `PONG` and a version number.

### 4.3 Both from the application itself

```bash
docker compose exec challenge-web python manage.py shell -c \
  "from django.db import connection; print(connection.vendor); from django.core.cache import cache; cache.set('k','v',30); print(cache.get('k')); from channels.layers import get_channel_layer; print(type(get_channel_layer()).__name__)"
```

Expected:

```
postgresql
v
RedisChannelLayer
```

`RedisChannelLayer` is the important line: it proves real-time messaging is
backed by Redis and not by the in-memory test transport.

---

## 5. Automated tests

```bash
docker compose exec challenge-web python manage.py test --verbosity 2
```

Expected: `Ran 66 tests ... OK`, with no `skipped` count.

---

## 6. Full verification

```bash
pwsh -File tests/scripts/verify.ps1
```

Expected: `VERIFICATION PASSED: every check succeeded.`

---

## 7. Isolation from other projects

```bash
docker compose ls
docker ps --format '{{.Names}}\t{{.Status}}'
```

Expected: `almanar-interactive-challenge` appears as its own project alongside
any other project on the machine; the other project's containers are still
running and were never restarted by this work.

---

## 8. Shutdown

```bash
docker compose down       # keeps the database
docker compose down -v    # deletes database, Redis and media volumes
```

Note that `down -v` destroys all competition data. Use it only for a clean
reinstall.