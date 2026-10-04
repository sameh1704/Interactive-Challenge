# Performance

Sizing, the levers that matter, and the figures that have actually been
measured. Every number in the measured section comes from a recorded run — there
are no estimates here, and no number is quoted for a configuration nobody ran.

For how to keep the system healthy day to day see
[OPERATIONS.md](OPERATIONS.md).

---

## What a "screen" costs

A screen is a long-lived WebSocket, not a poll. While it is connected it holds:

- one socket and its buffers in Daphne's event loop,
- one Redis channel-layer group membership, kept alive by a ping every
  `LIVE_HEARTBEAT_SECONDS` (15 s) and a client-side heartbeat,
- one screen row in PostgreSQL, touched once per heartbeat.

It costs **nothing per second while idle** beyond those two beats. The expensive
moments are the teacher's actions, where one broadcast fans out to every
connected screen at once:

| Teacher action | Cost |
| --- | --- |
| Start / next question | One query, then one small message per screen |
| Answer submission | One row per classroom; **not** broadcast to other screens |
| End question (reveal) | The largest payload: the answer key **plus the whole leaderboard**, so it grows with the number of classrooms in the round |
| Leaderboard after the reveal | Travels inside the reveal payload rather than as its own event |

That last row is the one to know about. A reveal for 20 classrooms sends 20 rows
to every screen. It is measured below.

---

## The levers

All of these are in `.env` and are documented there.

| Setting | Default | What it trades |
| --- | --- | --- |
| `LIVE_HEARTBEAT_SECONDS` | 15 | Lower keeps subscriptions alive more cheaply; higher means a longer worst case before a genuinely dead screen is noticed |
| `CHANNEL_LAYER_EXPIRY` | 300 | The backstop only. **Must stay well above the heartbeat** — see below |
| `SCREEN_ONLINE_WINDOW_SECONDS` | 90 | How long a screen is shown as online after its last heartbeat |
| `CHANNEL_LAYER_CAPACITY` | 1500 | Messages buffered for a group before the oldest is dropped. Far above any realistic school |
| `NGINX_RATE_LIMIT_PER_IP` | 30r/s | Proxy-side burst control |
| `NGINX_RATE_LIMIT_BURST` | 120 | |
| `LIVE_TICK_INTERVAL_SECONDS` | 5 | How often a screen gets a server-clock beacon |
| `SCREEN_RATE_LIMIT` | 120/min | Credential guessing brake; generous on purpose |

### The expiry trap

`CHANNEL_LAYER_EXPIRY` **must** be well above `LIVE_HEARTBEAT_SECONDS`. The
heartbeat is what keeps a screen's group membership alive; the expiry is only the
backstop for a screen that has gone away. Set the expiry near the heartbeat and
an idle screen's subscription lapses between beats — and a screen that is silently
unsubscribed keeps displaying the last question it received, with nothing on the
board to say it is stale. That is worse than a visible error.

This relationship is asserted by
`core.tests.test_deployment_config.EnvExampleTests`, so the shipped defaults
cannot drift into it.

---

## Measured

Recorded on the development machine, **not** on the target server:

| | |
| --- | --- |
| Host | Intel Xeon E5-2680 v4, 14 cores / 28 threads, 63.9 GB RAM |
| Docker | 29.6.2, Desktop on Windows |
| Application | Single Daphne process (see below), development settings |
| PostgreSQL / Redis | Same host, containerised |
| Clients | **Simulated on the loopback interface of the same host.** No VLAN, no network |

Command, run inside the container:

```bash
python -m live.tests.live_load_check --screens 20
```

### Results

Every run drove one real competition end to end: start, question 1, all screens
answer, reveal, leaderboard, question 2, reveal, finish, and the frozen standings
read back out of the database. Every run passed every check.

| Screens | Connect (median / p95 / max) | Question broadcast (median / p95) | Reveal + leaderboard (median / p95) | Last answer accepted |
| --- | --- | --- | --- | --- |
| 5 | 29 / 32 / 32 ms | 42 / 42 ms | 104 / 104 ms | 126 ms |
| 10 | 27 / 33 / 33 ms | 62 / 62 ms | 164 / 164 ms | 208 ms |
| 20 | 28 / 34 / 61 ms | 41 / 41 ms | 348 / 348 ms | 441 ms |

### Resources

Sampled with `docker stats` on the `challenge-web` container during the 20-screen
run:

| | CPU | Memory |
| --- | --- | --- |
| Idle | 0.03 % | 78.7 MiB |
| **Peak during a 20-screen round** | **115.7 %** | **157.4 MiB** |
| Immediately after | 0.05 % | 80.2 MiB |

CPU above 100 % is normal: Docker reports across all cores, and the run is
short and bursty. Peak memory is under 160 MiB.

### Reading these numbers honestly

Three limits on what they prove:

1. **Clients on the loopback interface.** This measures the application and the
   two datastores. It measures **no** network. Latency across a switch or a
   routed VLAN is not included and cannot be inferred from these figures.
2. **The harness is sequential.** Each screen's arrival is awaited in turn, so
   the reported per-screen figures include the harness's own scheduling. Treat
   them as an upper bound on delivery, not a precise per-screen latency.
3. **Simulated screens, not browsers.** They speak the WebSocket protocol
   directly and do nothing a real browser does — no rendering, no timers between
   frames, no reconnection logic. A real fleet will be less efficient per client
   and more realistic in its connection churn.

The shape of the result is the useful part: 5 → 20 screens roughly **quadruples
the reveal time** while the connect time stays flat, which is what the reveal
payload growing with the number of classrooms predicts. Question broadcast stays
flat because that payload does not grow.

---

## Single process, and why

Daphne is single-process: this version has no multi-worker flag, and a single
event loop keeps the ordering of a live round easy to reason about. `restart:
unless-stopped` covers the failure case — if the process dies, the container
restarts and every screen reconnects on its own. What a restart means for a round
in progress is in [OPERATIONS.md](OPERATIONS.md).

The measurements above are therefore the numbers for **one** process. A school
fleet fits inside that comfortably; the point at which it would not is well beyond
what a single school runs, and it is not the constraint to design for.

---

## Sizing guidance

| Question | Answer |
| --- | --- |
| How many screens can one server hold? | Comfortably a whole school's worth. 20 concurrent screens used under 160 MiB and finished every check. |
| What grows with the number of screens? | The reveal payload, linearly in the number of classrooms in the round. |
| What grows with the number of classrooms in a competition? | The leaderboard in every reveal. Splitting a very large round into phases is the lever if a reveal ever feels slow. |
| What happens if a screen disconnects mid-question? | It is dropped from the round and reconnects on its own with backoff; its classroom keeps its score. |
| Is the database a bottleneck? | Not at school scale. One row per classroom per question, written once. |

---

## What has **not** been measured

- Cross-VLAN network latency. See [PILOT.md](PILOT.md) — **not executed**.
- Behaviour on the target Ubuntu server, or on school-server hardware.
- Sustained load over a full lesson or a full school day. These runs are seconds
  long; a day is hours.
- More than 20 concurrent screens. No attempt was made to find a ceiling, so no
  ceiling is claimed.
- Report generation over a large historical dataset.

Do not read the absence of a number as a passing result.
