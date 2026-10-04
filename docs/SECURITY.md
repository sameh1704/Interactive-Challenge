# Security

The security model of the Challenge system, what enforces each part of it, and
what is deliberately *not* defended. Written for an administrator deciding what
to allow on the school network, and for a reviewer checking the claims.

Read [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) for the firewall rules
and [OPERATIONS.md](OPERATIONS.md) for day-to-day running.

---

## The trust boundary in one paragraph

Everything authoritative happens on the server. There are exactly **two secrets a
client ever presents**: a **Screen ID**, and a **password**. There is no third:
no API token, no client certificate, no shared key between screens. Both secrets
are guesses that a script can automate, so both are counted per client address.

The browser UI is not a security boundary. A screen can do anything a browser
could do from the same page; what stops it is that the page offers nothing worth
stealing and the server checks every write.

---

## The two secrets

### The Screen ID

A Screen ID such as `AM-7KQ4XB` is a bearer credential. It authorises that
screen's classroom into a live competition, and nothing else — but "into a live
competition" is enough to matter, so its presentation is rate limited.

| Control | Where |
| --- | --- |
| Unknown and inactive Screen IDs produce the **same** response | `backend/screens/views.py` |
| Presentation counted per client address | `backend/core/ratelimit.py` |
| Applied to the status page, the heartbeat **and** the WebSocket | `screens/views.py`, `live/views.py`, `live/consumers.py` |
| Defaults: 120 presentations per address per 60 s | `SCREEN_RATE_LIMIT`, `SCREEN_RATE_LIMIT_WINDOW` |

The limit is deliberately generous. A classroom sits behind one NAT address, each
screen reconnects on its own, and a screen page load plus a heartbeat is two
presentations. 120/minute is well above a whole room's real traffic.

Being throttled and being wrong produce the **same page**; only the status
differs (429 with `Retry-After`, versus 404). That difference carries no
information about any Screen ID — it says something about the *caller*, not about
whether the ID exists.

### The password

The password is the only thing standing between the school network and the
teacher controls, the question bank and the championships.

| Control | Where |
| --- | --- |
| Counted **before** it is checked | `backend/accounts/forms.py` |
| Per username, globally | `core.ratelimit.login_allowed` |
| Per client address, at `LOGIN_RATE_LIMIT × LOGIN_RATE_LIMIT_ACCOUNT_FACTOR` | same |
| A wrong password and an unknown username cost the same | `SignInForm.clean` |
| Failures logged with username and source address, never the password | `SignInForm.clean` |

**The quota is on attempts, not failures.** A *successful* sign-in consumes it
exactly as a wrong password does, and once the quota is spent, further attempts in
the window are refused **without the password being checked** — including a
correct one. This is deliberate: whether an attempt would have succeeded is what
an attacker controls, so it must not change what an attempt costs. The lockout
ends when the window passes.

At the shipped default (10 attempts per 300 s) this means a shared staff-room
machine cannot sign in more than ten times inside five minutes. Raise
`LOGIN_RATE_LIMIT` if a room genuinely needs to; lower it only after watching
the logs for real throttling.

---

## The one thing to get right: `X-Forwarded-For`

This is the single most security-relevant line in the deployment.

In production the application runs behind its own nginx, and the proxy's address
is not the screen's. So the application is told to trust the forwarded address —
but **only safe because the proxy overwrites the header**:

```nginx
proxy_set_header X-Forwarded-For   $remote_addr;
```

`challenge-web` publishes **no host port** (`ports: !reset []` in
`docker-compose.prod.yml`), so every request, and therefore every WebSocket
upgrade, passes through that proxy and `remote_addr` is the real client.

The failure mode if this is ever changed to `$proxy_add_x_forwarded_for` is not
subtle: appending leaves whatever the client sent in the left-most position,
which is the position `screens.network.client_ip` reads. A client could then send

```
X-Forwarded-For: <random>
```

on every request, be counted under a fresh address each time, and walk straight
through the Screen ID rate limit — while also forging the address shown in the
screen list.

Both halves of that coupling are pinned by tests in
`backend/core/tests/test_deployment_config.py`, because neither is visible from a
Python import: the Django test client does not run nginx.

If another proxy is ever placed in front of this one, it must overwrite the header
too, or `SCREEN_TRUST_FORWARDED_FOR` goes back to `False`.

---

## What a screen cannot do

The screen endpoints are unauthenticated by necessity — nobody sits at a screen.
The model is instead:

- A screen sees **only its own record**: name, Screen ID, status, and its
  classroom's name.
- A screen writes **only** `last_seen`, `ip_address` and `browser_user_agent`.
  Identity, classroom and role are not writable, so a heartbeat cannot move a
  screen into another classroom.
- No teacher account, no other screen, no administration function is reachable
  from there. Registration and deactivation happen in the Django admin, which only
  administrators can enter.

See the module docstring in `backend/screens/views.py`.

---

## Authorisation

| Boundary | Rule |
| --- | --- |
| Teacher / administrator | Session-based, `ADMINISTRATOR.md` |
| Every admin site | Uses `AdministratorAdminMixin`; staff who are not administrators see nothing |
| Tournament management | The owning teacher or an administrator; report queries enforce the same boundary |
| Reports | Scoped to the classrooms and competitions the requester may see |
| Uploaded images | Served by the proxy from a read-only mount, never executed by Django |

---

## Data protection in transit and at rest

Production sets `DEBUG=False` regardless of `.env`, and
`config.settings.production` **refuses to start without `DJANGO_SECRET_KEY`**.

Cookie and HSTS settings default to **secure**, which is correct only once HTTPS
is confirmed on the target server — with `SESSION_COOKIE_SECURE=True` over plain
HTTP the browser refuses to send the session cookie and nobody can sign in. This
is a deployment decision, not a code defect: see the matrix in
`OPERATIONS.md`, and enable it deliberately rather than by assumption.

PostgreSQL and Redis are **not** published to the host in the production stack.
Only the proxy's port 80 is reachable from the network.

Uploaded question images are user-supplied files. They are served by nginx with
`X-Content-Type-Options: nosniff` from a read-only volume, and never passed
through Django.

---

## What is deliberately not defended

Stated plainly, because an unstated gap is worse than a known one.

| Not defended | Why |
| --- | --- |
| Brute force against a **known** Screen ID from a **rotating** pool of source addresses | The Screen ID is 8 characters of base32 (~40 bits). Bounded only by the per-address counter and the proxy's `limit_req`. See the pilot in `PILOT.md` before relying on it. |
| Screen ID entropy | Generated as `AM-` plus 6 base32 characters. Lengthening it is a schema and screen-setup change, not a configuration one. |
| Physical access to a screen or a teacher device | Out of scope. |
| A malicious administrator | Out of scope. |
| Traffic analysis of who answered what | Not attempted. |
| Denial of service by a determined attacker on the LAN | Partly mitigated by `limit_req`, the proxy burst control, and the application limiter. Not fully. |

---

## Secrets handling

- `.env` is git-ignored and holds the real values. **`.env` must never be
  committed.**
- `docker-compose.prod.yml` contains **no** credential. Every value is
  `${VAR:?message}` (required) or `${VAR:-default}`.
- `deploy/backup.sh` reads the database password from `.env` and passes it with
  `docker exec -e PGPASSWORD=...`, so it never appears in the shell history or in
  the process list.
- `backups/` is git-ignored. A custom-format dump is a copy of the whole
  database **including password hashes**, so `git add -A` must never pick one up.
  This is enforced by `.gitignore` (`backups/`, `*.dump`, `*.dump.sha256`).

Generate the secret key with:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

---

## The audit trail

Two log lines matter, both from `LOGIN_RATE_LIMIT`-related code paths:

```
WARNING accounts.forms Sign-in refused: rate limit reached for 'teacher.one' from 10.0.0.9.
WARNING accounts.forms Failed sign-in for 'teacher.one' from 10.0.0.9.
WARNING live.teacher_consumer Competition control refused (invalid_state): ...
```

A wrong password and an unknown username produce **one identical line**, so the log
cannot be used to enumerate accounts. The password is never logged, anywhere.

To review sign-in activity:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs challenge-web \
  | grep -E "accounts.forms|ratelimit"
```

---

## Reporting a security problem

Do not open a public issue. Tell the school's system administrator, who has the
server access and the backup history needed to assess impact. For a suspected
compromise: take a backup first, then check the audit trail above, then rotate the
affected credentials in `.env` and redeploy.
