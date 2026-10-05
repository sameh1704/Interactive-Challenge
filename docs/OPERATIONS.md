# Operations

Running the server: health, logs, updates, rollback and emergency recovery.
Administrator-level; no programming.

---

## Health checks

| Endpoint | What it tells you | Use |
| --- | --- | --- |
| `/health/` | The application **and** its dependencies | uptime monitoring |
| `/proxy-health` | The reverse proxy only | confirms the proxy is up |

`/health/` returns **200** when everything is reachable and **503** when a
dependency is not, so it works as a monitor without parsing the body:

```bash
curl -fsS http://<server>/health/
```

```json
{
  "status": "ok",
  "environment": "production",
  "checks": {
    "database": { "status": "ok", "engine": "postgresql", "latency_ms": 18.5 },
    "redis":    { "status": "ok", "latency_ms": 8.2 }
  }
}
```

**This is the distinction that matters in an incident:**

| Result | Meaning | What to do |
| --- | --- | --- |
| `status: "ok"` | The application and every dependency are fine | Nothing |
| `503`, `database: unavailable` | The application is running; **PostgreSQL** is not | Database section below |
| `503`, `redis: unavailable` | The application is running; **Redis** is not | Live rounds will fail; HTTP pages still work |
| Connection refused / timeout | The application or proxy is not running | Start it |

A dependency failure is never reported as an application crash, and the reason
given is generic — internal host names and credentials are never returned to a
client. The detail is in the logs.

---

## Logs

Everything goes to stdout, so `docker logs` is the whole story:

```bash
docker logs --tail 200 almanar-challenge-web
docker logs -f almanar-challenge-web          # follow
docker logs --since 1h almanar-challenge-web   # last hour
docker logs almanar-challenge-proxy           # access log, WebSocket upgrades
```

Set `LOG_LEVEL=DEBUG` in `.env` and recreate the web container for more detail.
Leave it at `INFO` in normal running.

### Diagnosing the four things that matter

**A screen will not connect.** The `channels.server` lines record each socket's
open, upgrade and close:

```bash
docker logs almanar-challenge-web 2>&1 | grep -iE 'websocket|upgrade|connection'
```

Also check the proxy, which records the upgrade attempts that never reached the
application:

```bash
docker logs almanar-challenge-proxy 2>&1 | grep -E ' 4[0-9][0-9] | 101 '
```

A screen that never appears in the application log reached the proxy but not the
app. A screen that appears and is immediately closed was refused by identity.

**Authentication failures.** Recorded with the username and the source address,
never with the password:

```bash
docker logs almanar-challenge-web 2>&1 | grep -E 'Failed sign-in|rate limit'
```

An address producing many failures is worth investigating.

**Server errors.** Tracebacks, with the environment name prefixed:

```bash
docker logs almanar-challenge-web 2>&1 | grep -A 20 'ERROR'
```

**Competition state failures.** A refused control action is returned to the
teacher *and* logged, so a round driven into an invalid state is visible
afterwards:

```bash
docker logs almanar-challenge-web 2>&1 | grep 'Competition control refused'
```

**What is never logged:** passwords, session cookie values, Screen IDs presented
by an unauthenticated client, or the contents of a successful request. The log is
safe to read, and safe to attach to a support request.

---

## Updating the application

**Take a backup first.** See [BACKUP-RESTORE.md](BACKUP-RESTORE.md).

```bash
cd /opt/almanar/challenge

# 1. What is running now, so you can go back to it.
git rev-parse HEAD | tee /tmp/challenge-previous-commit

# 2. Get the new version.
git fetch
git checkout <the-tag-or-branch>

# 3. Rebuild and restart. The prepare service applies migrations and
#    collects static files before the application serves traffic.
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Then verify:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl -fsS http://<server>/health/
docker logs almanar-challenge-prepare
```

Open the application in a browser and confirm a round can be started.

### Never do this during a lesson

An update recreates the web container, which drops every screen's connection.
Screens reconnect on their own, but a round in progress is disrupted. Update
between lessons.

### Database migrations

Migrations run automatically at startup. A migration that fails leaves the schema
part-updated — the web container will not start and `/health/` will report the
database as unavailable. That is the safe direction: better than serving a
half-migrated schema. Roll back (below) or restore from backup.

---

## Rollback

If an update breaks something, go back to the previous version:

```bash
cd /opt/almanar/challenge
git checkout $(cat /tmp/challenge-previous-commit)
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
curl -fsS http://<server>/health/
```

**A code rollback does not undo a migration.** If the new version applied a schema
change, the database is now ahead of the old code. Either:

1. Reverse the migration, if it is reversible — check for a `RunPython` with a
   reverse function, then
   `docker exec -it almanar-challenge-web python manage.py migrate <app> <number>`, or
2. **Restore the database from the backup taken before the update.** This is the
   certain route, and it loses any data created since.

Take that backup before every update for exactly this reason.

---

## Emergency recovery

### The server is down

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Restart policies are `unless-stopped`, so this should rarely be needed — but it
is the first thing to try.

### `/health/` reports the database unavailable

```bash
docker logs almanar-challenge-db 2>&1 | tail -50
docker exec almanar-challenge-db pg_isready -U challenge -d challenge
```

**`password authentication failed for user "challenge"`** almost always means
`DATABASE_PASSWORD` in `.env` no longer matches the password PostgreSQL was
initialised with. PostgreSQL applies that variable only on first start. Either
restore the original password, or change it in the database — see the warning in
[DEPLOYMENT.md](DEPLOYMENT.md) § 4.

### `/health/` reports Redis unavailable

Live competitions cannot run without it. Check the container and its disk space:

```bash
docker logs almanar-challenge-redis 2>&1 | tail -30
docker system df
```

### The database volume looks wrong

**Stop the application first**, then inspect. Do not repair a running database.

```bash
docker stop almanar-challenge-proxy almanar-challenge-web
docker exec -it almanar-challenge-db psql -U challenge -d challenge
```

### Everything is lost

1. Leave the containers down.
2. Install the previous version (above).
3. Restore the most recent good backup — [BACKUP-RESTORE.md](BACKUP-RESTORE.md).
4. Bring the stack up and verify `/health/`.
5. Re-register any screens whose records did not survive.

### Disk full

```bash
docker system df
```

Prune build cache and stopped images, **never** volumes:

```bash
docker image prune -a
docker builder prune
```

`docker volume prune` would delete the database. Do not run it.

---

## Running the verification suite

The application image carries its own tests, so a deployed server can check
itself:

```bash
docker exec almanar-challenge-web python manage.py test
docker exec almanar-challenge-web python manage.py check
docker exec almanar-challenge-web python manage.py makemigrations --check --dry-run
```

Live checks, against the running stack. **Set the host to the address the
application is reached at in production, not the proxy's container name** —
see "Why the address, not the container name" below:

```bash
# Replace 172.16.1.2 with the deployment's own address, from ALLOWED_HOSTS.
SERVER=172.16.1.2

# Three classrooms plus a teacher, over real sockets.
docker exec -e LIVE_CHECK_HOST=$SERVER -e LIVE_CHECK_PORT=80 \
  almanar-challenge-web python -m live.tests.live_multi_client_check

# Reveal behaviour, including the server clock reaching a deadline.
docker exec -e LIVE_CHECK_HOST=$SERVER -e LIVE_CHECK_PORT=80 \
  almanar-challenge-web python -m live.tests.live_reveal_check

# A full round at classroom scale.
docker exec -e LIVE_CHECK_HOST=$SERVER -e LIVE_CHECK_PORT=80 \
  almanar-challenge-web python -m live.tests.live_load_check --screens 20 \
  --host $SERVER --port 80 --allow-remote-host --allow-production-settings
```

The load check writes to the database it is pointed at, so it refuses to run
against a non-local host or under production settings until both are explicitly
allowed. On a deployment with real competition results, run it on a rehearsal
database instead.

#### Why the address, not the container name

Pointing these at the proxy's container name — `almanar-challenge-proxy` — looks
like it should work, and does not. The check connects to that name on port 80,
which reaches nginx, and nginx forwards the request with `Host:
almanar-challenge-proxy`. Django then applies `ALLOWED_HOSTS`, which lists the
addresses the school actually uses and not Docker's internal names, and refuses
the request with **HTTP 400**.

This is the correct behaviour and must stay that way: `ALLOWED_HOSTS` is a
security control that stops one school server answering requests meant for
another, and adding container names to it to make a test convenient would widen
the accepted set for every request. Use the deployment address.

To reach Daphne directly, bypassing the proxy, use `127.0.0.1:8000` from inside
the container — but that skips the nginx WebSocket upgrade path, so it does not
prove the proxy works. Prefer the address above.