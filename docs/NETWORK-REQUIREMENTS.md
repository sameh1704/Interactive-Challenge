# Network Requirements

What the network team needs to know, and what has and has not been tested.

Read this before the firewall rules are written. Everything here is expressed as
source, destination, protocol and port so it can be transcribed directly.

---

## The requirement in one sentence

Every interactive screen and every teacher device must be able to make **outbound
TCP connections** to **one** server address, on **one** port, for ordinary web
traffic **and** for a persistent WebSocket. Nothing else is needed.

---

## Ports

| Source | Destination | Protocol / port | Purpose | Direction |
| --- | --- | --- | --- | --- |
| Any screen VLAN | Challenge server | **TCP 80** (or 443 with HTTPS) | Application pages, answer submission, **and the WebSocket** | Outbound from the client |
| Teacher/admin VLAN | Challenge server | **TCP 80** (or 443) | Sign-in, competitions, reports | Outbound from the client |

That is the complete list.

### The WebSocket uses the same port

The screen's live connection is a WebSocket to:

```
ws://<server>/ws/live/screen/?screen_id=AM-XXXXXX
ws://<server>/ws/live/teacher/<competition-id>/
```

It is a **WebSocket upgrade on the same port as the web pages** — not a separate
port. A firewall rule that permits `TCP 80` for HTTP already permits the upgrade,
because the upgrade is an HTTP request with two extra headers
(`Upgrade: websocket`, `Connection: Upgrade`).

There is no port 8000, no port 8080, no separate WebSocket port, and no UDP.

---

## What must NOT be required

None of the following are needed, and none of them are used:

- **No same-VLAN requirement.** Screens may be in any building.
- **No Layer 2 adjacency.** Nothing is on the same segment as the server.
- **No broadcast or multicast discovery.** There is no discovery protocol.
- **No peer-to-peer traffic between screens.** A screen talks only to the server.
  Screens do not see each other.
- **No inbound connection to the server from outside.** The server initiates
  nothing to client devices.
- **No VPN, no overlay network, no spanning tree.**

## Why no discovery is needed

A screen identifies itself by a **Screen ID** printed in its URL — for example
`/live/screen/?screen_id=AM-7KQ4XB`. That is a value the school assigns to that
physical screen, not something derived from the network.

The server records a screen's IP address, but only as **monitoring data**. It is
never used to decide who a screen is. Consequences worth knowing:

- Re-addressing a screen by DHCP changes nothing. It keeps its Screen ID, its
  classroom and its history.
- Two screens behind one NAT address are separate devices and stay separate.
- A screen may move to a different building and keep working.

This is deliberate. If identity came from the IP address, a DHCP lease change
could silently move a room's participation in a competition to a different
device — during a lesson, with no visible error.

## Addressing

Use whatever name the school already resolves; no new DNS is required. Both work:

```
http://challenge-server/          # by name
http://172.16.1.20/               # by address
```

Whatever is used **must be listed in `ALLOWED_HOSTS`** in `.env`, because that
is the name the browser sends and the server validates it. A mismatch is
refused — the symptom is a "Bad Request" or an empty page, not a timeout, which
makes the cause easy to find.

Prefer one URL for users and screens. Do not give staff a second address.

## Reverse proxy and TLS

The stack terminates HTTP at its own nginx container, which forwards to the
application on the private Docker network. TLS may be terminated:

1. **At this nginx**, by adding a certificate and a `listen 443 ssl` server
   block (see `deploy/nginx/templates/challenge.conf.template`), or
2. **At an existing school load balancer or firewall** in front of this one, in
   which case set `SCREEN_TRUST_FORWARDED_FOR=True` (the production compose file
   already does) and add `CSRF_TRUSTED_ORIGINS=https://<public-name>`.

The application reads `X-Forwarded-Proto` (`SECURE_PROXY_SSL_HEADER`), so it
knows the original scheme and will not redirect in a loop.

---

## Verification from a client machine

Run these from a screen or teacher device **in each VLAN** you want to support.
They prove routing and DNS without needing the application to be in use.

```powershell
# Windows
Test-NetConnection -ComputerName challenge-server -Port 80
Resolve-DnsName challenge-server
```

```bash
# Linux
nc -vz challenge-server 80
getent hosts challenge-server
curl -fsS -o /dev/null -w '%{http_code}\n' http://challenge-server/health/
```

Expect `TcpTestSucceeded: True` and HTTP `200`.

To prove the WebSocket specifically, from a client machine:

```bash
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
     http://challenge-server/ws/live/screen/?screen_id=AM-XXXXXX
```

`101 Switching Protocols` means the upgrade survived the whole path. Any other
status is the exact thing to report (see below).

---

## If connectivity fails

Report these four facts and no investigation is needed to start:

| Item | Value to report |
| --- | --- |
| **Source** | the failing device's IP address and VLAN |
| **Destination** | the Challenge server's IP address |
| **Protocol** | TCP |
| **Port** | the port in use (80, or 443 with HTTPS) |

Also report, if known:

- Whether DNS resolved (`Resolve-DnsName`)
- Whether `Test-NetConnection` says `TcpTestSucceeded`
- The exact HTTP status the WebSocket upgrade returned
- `curl -v http://<server>/health/` output

**Do not change VLAN design or routing to fix this.** A failing screen is a
network problem, and this document exists so it can be diagnosed rather than
worked around.

### Common causes, in the order worth checking

| Symptom | Likely cause |
| --- | --- |
| DNS does not resolve | Name not in DNS, or the wrong name in `PUBLIC_BASE_URL` |
| `TcpTestSucceeded: False` | Firewall between the VLANs, or the proxy not listening |
| Connects, then `400`/`Bad Request` | The address used is not in `ALLOWED_HOSTS` |
| Pages load, sockets never connect | Something is terminating HTTP and dropping `Upgrade` |
| Pages load, sockets drop every ~60s | An intermediary idle timeout; see `NGINX_PROXY_READ_TIMEOUT` |
| Intermittent, one VLAN only | Routing or a duplicate address, not the application |

---

## What still has to be tested on the real network

**This has not been done.** The application was verified on a development
machine: the full automated suite, the networked live checks, and load runs at 5,
10 and 20 screens — all of which used clients on the same host as the server.
The deployment configuration is validated but has **not** been run on the target
Ubuntu server. See [README.md](README.md) § Status.

The following require the actual school network and are **outstanding**:

- [ ] A real screen in **screen VLAN A** opens the application
- [ ] A real screen in **screen VLAN B** opens the application
- [ ] Both connect their WebSocket
- [ ] A teacher starts a competition and both screens show the same question
- [ ] Both answer, scores are correct, the leaderboard updates
- [ ] End Question, result reveal, Next Question and final result all work
- [ ] A third building VLAN, if one exists

Until at least two screen VLANs are tested, do not treat the system as proven on
the school network. The design requires no VLAN-specific behaviour, and the
pilot is expected to pass; that is an expectation, not a result.

See `docs/PILOT.md` for the procedure to run when the network is available.