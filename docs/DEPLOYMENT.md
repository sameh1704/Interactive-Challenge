# Deployment — Ubuntu Server

Installing the Challenge application on the school server. Read
[NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) first so the firewall rules
can be requested at the same time.

Nothing here touches the school network by itself. It installs containers on the
server. VLANs, routing and firewall rules are configured by the network team from
the requirements document.

---

## 1. What gets installed

Five containers on one server:

| Service | Purpose | Exposed to the network |
| --- | --- | --- |
| `challenge-proxy` | nginx. The single address everything uses. | **Yes — one port** |
| `challenge-web` | Django and Daphne (HTTP + WebSocket) | No |
| `challenge-db` | PostgreSQL. The only permanent store. | No |
| `challenge-redis` | Cache and the WebSocket message bus | No |
| `challenge-prepare` | Runs migrations and collects static files, then exits | No |

Only the proxy publishes a port. PostgreSQL and Redis are reachable solely on the
project's private Docker network, so nothing on the school network can address
them directly.

```
                    school network (any VLAN, routed)
                              │
                    ┌─────────┴──────────┐
                    │  challenge-proxy   │   nginx  ← the only published port
                    └─────────┬──────────┘
                              │ private docker network
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        challenge-web   challenge-db    challenge-redis
        Django+Daphne    PostgreSQL     cache/bus
              │
              ▼
        uploaded question images
        (challenge-media volume)
```

---

## 2. Prerequisites

On the server:

```bash
docker --version          # 24.0 or newer
docker compose version    # v2.20 or newer
git --version
```

If Docker is not installed, follow Docker's official Ubuntu instructions, then
confirm the current user can run containers without `sudo`:

```bash
docker run --rm hello-world
```

---

## 3. Get the code

```bash
sudo mkdir -p /opt/almanar
sudo chown "$USER" /opt/almanar
git clone <repository-url> /opt/almanar/challenge
cd /opt/almanar/challenge
```

---

## 4. Create the production `.env`

```bash
cp .env.example .env
python3 -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Put the printed value in `DJANGO_SECRET_KEY`, then work through the file. The
values that **must** be changed from the example:

| Variable | Value | Notes |
| --- | --- | --- |
| `DJANGO_ENV` | `production` | |
| `DJANGO_SETTINGS_MODULE_NAME` | `production` | |
| `DJANGO_SECRET_KEY` | the generated value | Never commit this |
| `DEBUG` | `False` | |
| `ALLOWED_HOSTS` | the server's name **and** address | See below |
| `DATABASE_PASSWORD` | a strong random password | Choose it now; see the warning |
| `SESSION_COOKIE_SECURE` | `True` | Only with HTTPS — see below |
| `CSRF_COOKIE_SECURE` | `True` | Only with HTTPS |
| `PUBLIC_BASE_URL` | `http://<server>:80` | The address staff will open |

### `ALLOWED_HOSTS`

The address teachers and screens will use, separated by commas:

```
ALLOWED_HOSTS=challenge-server,172.16.1.20
```

Use the server's DNS name **and** its IP address if they will both be used. This
list is a security control: a request whose `Host` header is not on it is
refused, which is what stops one school server answering requests meant for
another. Do not use `*`.

Nothing is hard-coded in the application. Put the real address in `.env` only.

### The `DATABASE_PASSWORD` warning — read once

PostgreSQL applies `POSTGRES_PASSWORD` **only when it initialises an empty data
directory**, which is the first `docker compose up`. After that the password lives
inside the database volume.

If you change `DATABASE_PASSWORD` later, PostgreSQL will still expect the old
one, and the application will fail to start with:

```
FATAL:  password authentication failed for user "challenge"
```

This is not a bug and no amount of restarting fixes it. Either put the original
password back, or change it *inside* PostgreSQL:

```bash
docker exec -it almanar-challenge-db psql -U challenge -d challenge \
  -c "ALTER USER challenge WITH PASSWORD 'the-new-password';"
```

Then update `.env` to match.

### Cookie flags and HTTPS

`SESSION_COOKIE_SECURE=True` and `CSRF_COOKIE_SECURE=True` tell the browser to
send credentials only over HTTPS. **With plain HTTP nobody will be able to sign
in.** They default to `True` in `.env.example` on the assumption you will use
HTTPS.

- Serving HTTPS directly from this proxy: set them `True`.
- Serving plain HTTP for now (a pilot, an isolated VLAN): set them `False` and
  turn them on when HTTPS is in front. Record that as outstanding work.

---

## 5. Build and start

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The `prepare` service runs migrations and collects static files once, then exits:

```bash
docker logs almanar-challenge-prepare
```

You should see `static files copied`, the migration list, and
`[prepare] static files collected and migrations applied`.

Check everything came up:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Every service should read `(healthy)`.

---

## 6. Verify

```bash
# Application health, including its dependencies
curl -fsS http://<server>/health/

# The proxy itself
curl -fsS http://<server>/proxy-health
```

`/health/` returns HTTP 200 when everything is reachable and **503** when a
dependency is not, so it is usable as a monitor:

```json
{
  "status": "ok",
  "environment": "production",
  "checks": {
    "database": { "status": "ok", "latency_ms": 18.5 },
    "redis":    { "status": "ok", "latency_ms": 8.2 }
  }
}
```

Then open the landing page in a browser and confirm it looks right. A WebSocket
check needs the application test image; see
[OPERATIONS.md](OPERATIONS.md) § "Running the verification suite".

---

## 7. Make it start on boot

Docker's restart policies are already `unless-stopped`, so containers come back
after a reboot. Confirm Docker itself starts first:

```bash
sudo systemctl enable --now docker
sudo systemctl status docker
```

---

## 8. First administrator account

```bash
docker exec -it almanar-challenge-web python manage.py createsuperuser
```

Then continue with [ADMINISTRATOR.md](ADMINISTRATOR.md).

---

## 9. What to do next

- [ADMINISTRATOR.md](ADMINISTRATOR.md) — accounts, classrooms, screens
- [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) — firewall and VLAN rules
- [BACKUP-RESTORE.md](BACKUP-RESTORE.md) — set up backups now, not later
- [OPERATIONS.md](OPERATIONS.md) — updates and rollback

## Note on running two copies

`docker-compose.yml` fixes the project name, container names and network name, so
**only one instance of this stack can run on a given Docker host.** A second
instance would reuse the network, and two containers would answer to the same
`challenge-db` name. For a second school, use a second server.

## Note on `docker compose config`

That command prints your resolved configuration, **including `DATABASE_PASSWORD`
and `DJANGO_SECRET_KEY` in clear text.** Do not paste its output into a ticket,
a chat or a document.