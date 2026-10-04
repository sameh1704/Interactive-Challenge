# Troubleshooting

Find the cause before changing anything.

---

## First: which layer?

```bash
curl -fsS http://<server>/health/          # application + dependencies
curl -fsS http://<server>/proxy-health     # the reverse proxy
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

| `/proxy-health` | `/health/` | Conclusion |
| --- | --- | --- |
| fails | fails | Proxy or network. Go to § A |
| ok | fails | Application or a dependency. Go to § B |
| ok | ok | The application is healthy. Go to § C |

---

## A. The proxy or the network

**`/proxy-health` fails.**

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker logs almanar-challenge-proxy 2>&1 | tail -30
```

| Log line | Cause | Action |
| --- | --- | --- |
| `bind() to 0.0.0.0:80 failed (98: Address in use)` | Something else holds port 80 | Change `PROXY_PORT` in `.env`, or stop the other service |
| `connect() failed (111: Connection refused)` upstream | The application container is not running | `docker compose ... up -d challenge-web` |
| `connect() failed (113: Host is unreachable)` | The proxy has a **stale upstream address** | `docker restart almanar-challenge-proxy` |
| no lines, but it is unreachable | Firewall or wrong address | See below |

> The `Host is unreachable` case has a known cause: Docker gives a recreated
> container a new address, and nginx normally notices. If it does not, a proxy
> restart fixes it. The configuration resolves the upstream address dynamically to
> prevent this; if you have replaced the shipped `deploy/nginx` template with a
> literal `upstream` block, you will reintroduce it.

**It works on the server but not from a screen.** The application is fine; the
network is not. Report to the network team:

- source IP and VLAN of the failing device
- destination IP of the server
- **protocol TCP, port 80** (or 443)
- whether DNS resolves
- `Test-NetConnection -ComputerName <server> -Port 80` output

**Pages load but the socket never connects.** Something between the client and
the application is handling HTTP but dropping the `Upgrade` header. Common
culprits: a caching proxy, an SSL-terminating appliance that needs WebSocket
support enabled, or a firewall with a separate rule for non-standard methods.

Test the upgrade directly — see
[NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) § Verification. `101 Switching
Protocols` means the upgrade survived; any other status is the thing to report.

---

## B. The application or a dependency

### `{"status": "unavailable", "checks": {"database": {"status": "unavailable"}}}`

The application is up; PostgreSQL is not.

```bash
docker ps -a --filter name=almanar-challenge-db
docker logs almanar-challenge-db 2>&1 | tail -50
docker exec almanar-challenge-db pg_isready -U challenge -d challenge
```

| Log | Cause | Action |
| --- | --- | --- |
| `password authentication failed for user "challenge"` | `.env` does not match the password the database was created with | See below |
| `database system is starting up` | Still recovering | Wait 30s and retry |
| `could not bind ... Address already in use` | A stale `postgres` process | `docker restart almanar-challenge-db` |
| No such container | It was removed | `docker compose ... up -d challenge-db` |

**On `password authentication failed`:** PostgreSQL applies `POSTGRES_PASSWORD`
only when it initialises an *empty* data directory — that is, on first start.
Changing the variable afterwards does nothing. Either restore the original
password in `.env`, or change the password in the database and then update
`.env`:

```bash
docker exec -it almanar-challenge-db psql -U challenge -d challenge \
  -c "ALTER USER challenge WITH PASSWORD 'the-new-password';"
```

### `{"checks": {"redis": {"status": "unavailable"}}}`

The application is up; Redis is not. **Live competitions will not work.** Ordinary
pages still load.

```bash
docker logs almanar-challenge-redis 2>&1 | tail -30
docker system df      # a full disk stops Redis writing
docker restart almanar-challenge-redis
```

### The container restarts in a loop

```bash
docker logs almanar-challenge-web 2>&1 | tail -40
```

The entrypoint runs, in order: wait for services, `manage.py check`, migrate,
then start the server. The last line before the exit says which stage failed.

| Line | Meaning |
| --- | --- |
| `error: ... is required` from `check` | A required environment variable is missing or empty. Compare `.env` against `.env.example`. |
| `ImproperlyConfigured` for `SECRET_KEY` | No `DJANGO_SECRET_KEY` in `.env`. It is required in production. |
| A database error during migrate | See the database section above |
| `StaticFilesStorage` manifest error | `collectstatic` did not run with production settings; see below |

### `Missing staticfiles manifest entry for '...'`

Production serves static files with hashed names from a manifest. If every page
fails this way, the manifest is missing.

```bash
docker run --rm --network almanar-interactive-challenge-net \
  -v almanar-interactive-challenge_challenge-static:/app/staticfiles \
  almanar-challenge-web:0.1.0 python manage.py collectstatic --noinput --clear
```

The usual cause is deploying by hand without the `prepare` service. It runs
`collectstatic` with the **production** settings module into a shared volume; the
image's own build-time copy has no manifest because the build uses development
settings.

### `/admin/` refuses every sign-in

Almost always the secure-cookie trap: `SESSION_COOKIE_SECURE=True` while the site
is served over plain HTTP. The browser will not send the cookie back.

```bash
grep -E 'SESSION_COOKIE_SECURE|CSRF_COOKIE_SECURE' .env
```

Set them to `False` for plain HTTP, or put HTTPS in front. Same cause if
`CSRF_COOKIE_SECURE=True` makes every form submission fail with "CSRF verification
failed".

---

## C. The application is healthy but something misbehaves

### A screen will not connect

```bash
docker logs almanar-challenge-web 2>&1 | grep -iE 'websocket|unregistered|refused'
```

| Log | Meaning | Action |
| --- | --- | --- |
| Nothing at all | The request never reached the application | § A — proxy or network |
| `presented an unregistered Screen ID` | Unknown or inactive ID | Check the URL and that the screen is Active |
| `Refused (CLOSE_UNAUTHORISED)` | Valid screen, but its classroom was not in the round, or the classroom is inactive | Add the classroom to the competition |
| `Teacher WebSocket refused: <code>` | Teacher not signed in, or not allowed to drive this competition | Sign in again; check ownership |

Unknown and inactive Screen IDs are refused identically on purpose, so the log
cannot tell you which — by design, so the endpoint cannot be used to enumerate
registered screens.

### Screens disconnect repeatedly

```bash
docker logs almanar-challenge-proxy 2>&1 | grep -c ' 499 '
```

If connections drop at a regular interval, something is closing idle WebSockets.
The shipped configuration sets `NGINX_PROXY_READ_TIMEOUT` to 600s, which is
generous for the application's own 15-second heartbeat. Raise it if an
intermediate device is more aggressive still.

### A screen shows a stale question

Its socket dropped and did not recover. Refresh the screen. If screens in one
building are affected while others are fine, it is that building's network.

### Sign-in fails for everyone at once

```bash
docker logs almanar-challenge-web 2>&1 | grep 'Failed sign-in' | tail -20
```

If many different addresses are failing, the password is probably wrong. If one
address is failing repeatedly, the rate limit is doing its job and clears within
five minutes.

### Everything is slow

```bash
docker stats --no-stream almanar-challenge-web almanar-challenge-db almanar-challenge-redis
curl -fsS http://<server>/health/     # the latency_ms fields
```

See [PERFORMANCE.md](PERFORMANCE.md).

---

## Collecting information for support

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker logs --tail 300 almanar-challenge-web
docker logs --tail 100 almanar-challenge-proxy
curl -fsS http://<server>/health/
docker exec almanar-challenge-web python -c "import django,os; \
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.production'); \
django.setup(); from django.conf import settings; \
print('django', settings.APP_VERSION)"
```

**Do not attach `.env`.** It contains the secret key and the database password. If
a log line seems to expose a credential, treat it as a defect and report it
rather than forwarding the file.