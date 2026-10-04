# Pilot — cross-VLAN acceptance on the school network

The procedure to run **before** the system is used in a real lesson across
buildings. Nothing here is a workaround for a network problem; it is a test.

> **Status: NOT EXECUTED.** No cross-VLAN test has been performed. Every
> checkbox below is unticked. Do not treat the system as proven on the school
> network until at least two screen VLANs have passed this procedure. See
> [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) § "What still has to be
> tested on the real network".

---

## What is being proven

The system uses **normal IP routing and nothing else**:

- the server on `172.16.1.0/24`,
- screens in one or more **screen VLANs**, in different buildings,
- one URL, one port, ordinary outbound TCP from each client.

There is **no** same-VLAN requirement, **no** Layer 2 adjacency, **no** broadcast
or multicast discovery, and **no** peer-to-peer traffic between screens. A screen
identifies itself by its **Screen ID**, which the school assigns to that physical
device — never by its IP address. A screen may be re-addressed by DHCP, or moved
to another building, and it keeps its Screen ID, its classroom and its history.

So the pilot is not testing a mechanism. It is testing that the school's routing,
DNS and filtering actually permit the traffic, at the ports, in the directions
the application uses.

---

## Before you start

| Requirement | Detail |
| --- | --- |
| A deployed server | [DEPLOYMENT.md](DEPLOYMENT.md) completed |
| At least two screen VLANs | Building A and Building B minimum |
| A real screen in each | Configured per [SCREEN-SETUP.md](SCREEN-SETUP.md), Screen ID noted |
| A teacher device | On any VLAN that will run competitions |
| A test competition | Real questions, real duration — this is not a smoke test |
| **Do not change** | No VLAN, firewall or routing change is part of this procedure |

Run the pilot outside lesson time. A failure here is information, not an
incident.

---

## Step 1 — routing and DNS, from each VLAN

From a machine **in the VLAN**, not from the server:

```powershell
# Windows
Resolve-DnsName challenge-server
Test-NetConnection -ComputerName challenge-server -Port 80
```

```bash
# Linux
getent hosts challenge-server
nc -vz challenge-server 80
curl -fsS -o /dev/null -w '%{http_code}\n' http://challenge-server/health/
```

Record, per VLAN:

- [ ] **Building A** — DNS resolves; `TcpTestSucceeded: True`; `/health/` returns `200`
- [ ] **Building B** — same
- [ ] **Building C** (if one exists) — same

If `/health/` returns `400`, the address used is not in `ALLOWED_HOSTS`. If the
connection fails, it is a filtering or routing problem — report it with the four
facts in [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) § "If connectivity
fails". Do **not** change the network design to make this pass.

---

## Step 2 — the WebSocket, from each VLAN

Pages loading does not prove the socket works: an intermediary that drops the
`Upgrade` header lets HTML through and silently breaks every screen. Test it
explicitly.

```bash
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
     http://challenge-server/ws/live/screen/?screen_id=<A-REAL-SCREEN-ID>
```

Expect `101 Switching Protocols`.

- [ ] **Building A** — `101 Switching Protocols`
- [ ] **Building B** — `101 Switching Protocols`

Anything else — `400`, `404`, `502`, or a connection that opens and closes — is the
exact status to report.

---

## Step 3 — the screen page actually renders

On the physical screen in each building, open the screen URL and confirm the page
draws, the clock ticks, and the status page reports the screen as online with the
correct classroom.

- [ ] **Building A** — screen page renders, status page online, correct classroom
- [ ] **Building B** — screen page renders, status page online, correct classroom

A screen that renders but never leaves "waiting" is the signature of a socket that
connected and was then dropped — check `NGINX_PROXY_READ_TIMEOUT` in
[OPERATIONS.md](OPERATIONS.md).

---

## Step 4 — a real competition across VLANs

This is the step that matters. Use screens from **at least two different VLANs**
in the **same** competition, and run it end to end.

- [ ] Teacher signs in from the teacher device
- [ ] Teacher starts a competition; the screen is selectable
- [ ] Both screens connect and appear in the connected-screens count
- [ ] **Start Question** — every screen in every building shows the same question
- [ ] The countdown is running on all of them
- [ ] Both screens answer (send a real answer from each, not the same one)
- [ ] The answer count updates on the teacher's dashboard
- [ ] **End Question** — the reveal appears on every screen simultaneously
- [ ] The leaderboard on every screen matches the teacher's
- [ ] **Next Question** works for a second question
- [ ] **Finish Competition** — the final result appears everywhere
- [ ] The stored standings in a **report** match what the screens showed

Record the observed spread: if one building's reveal lagged noticeably behind the
other, write down roughly how much. That number is worth having.

---

## Step 5 — a classroom at full size

Repeat step 4 with **every screen in one classroom** connected at once, not just
one or two.

- [ ] A whole classroom's screens connect and all appear online
- [ ] All answer within the question duration
- [ ] No screen is dropped or shows a stale question
- [ ] The connection survives a pause between questions (idle for a few minutes,
      then Next Question still reaches every screen)

A screen that goes stale between questions is the `CHANNEL_LAYER_EXPIRY` failure
mode described in `.env.example`; it must be well above the heartbeat interval.

---

## Step 6 — the result

Record, in writing, in the school's own log:

| Item | Value |
| --- | --- |
| Date and who ran it | |
| Server address and VLAN | |
| Screen VLANs tested | |
| Screens per VLAN | |
| Any step that failed, with its exact error | |
| Observed reveal lag between buildings | |
| Firewall rules that had to be added, if any | |

Then update the two checkboxes in [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md)
and the status line in [README.md](README.md).

---

## If a step fails

1. **Do not change the network.** Record the failure with the four facts from
   [NETWORK-REQUIREMENTS.md](NETWORK-REQUIREMENTS.md) and the exact HTTP status.
2. **Check the server side first** — it is much cheaper to rule out:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
   docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail 100 challenge-web
   curl -fsS http://127.0.0.1/health/ | head -40
   ```
   Then [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
3. **Only then** escalate to the network team with the recorded facts.

A screen that works on the server VLAN and not in Building B is a network
problem by definition: the application has no VLAN-specific behaviour to get
wrong.

---

## What this pilot does not cover

- Wireless screens, or a building with no wired port for the screen.
- Two competitions running at the same time in different halls.
- A building that is reachable only through a router the school also uses for
  internet traffic.
- Behaviour during a link failure mid-question. That is an operations question;
  see [OPERATIONS.md](OPERATIONS.md) § "If a screen drops mid-lesson".
