"""The keepalive that holds a live round together.

Why this exists
---------------
A classroom screen stays connected for a whole lesson, and the gap between two
teacher actions can be minutes. But a channel-layer group membership is not
permanent: ``channels_redis`` gives each membership a TTL, and once it lapses
the channel is no longer in the group - so a screen that has been sitting still
silently stops receiving broadcasts. Nothing reconnects it, nothing reports an
error, and the board in the classroom quietly shows the previous question while
the teacher's dashboard has moved on. That is the worst failure this product
can have, and it happens to exactly the screens that are behaving correctly.

So the server keeps talking. Every :data:`HEARTBEAT_INTERVAL_SECONDS` each
attached round is sent a ``tick`` carrying the server clock and the current
state. That does three jobs at once:

* it refreshes the group membership, so an idle screen stays in its group;
* it keeps every screen's clock corrected against the server, which is the
  same beacon a screen can request on demand with ``ping``;
* it proves the socket is alive to the load balancer in front of it.

The task is started on demand by the consumers and runs once per process. It is
keyed by nothing but the process, because the set of live rounds it serves is
whatever the consumers in this process are attached to.
"""

from __future__ import annotations

import asyncio
import logging

from channels.db import database_sync_to_async
from django.conf import settings

from live import groups
from live.presence import LOCAL_MEMBERS

logger = logging.getLogger(__name__)

_TASK: asyncio.Task | None = None


def heartbeat_interval() -> float:
    """Seconds between keepalive broadcasts."""
    return float(getattr(settings, "LIVE_HEARTBEAT_SECONDS", 15))


def is_running() -> bool:
    return _TASK is not None and not _TASK.done()


async def ensure_running(channel_layer) -> None:
    """Start the keepalive for this process, if it is not already running."""
    global _TASK

    if is_running():
        return

    _TASK = asyncio.create_task(_run(channel_layer))


def stop() -> None:
    """Stop the keepalive. Used between tests and on shutdown."""
    global _TASK

    task, _TASK = _TASK, None
    if task is not None and not task.done():
        task.cancel()


def live_competition_ids() -> set[int]:
    """Competition ids this process currently has screens attached to.

    Derived from the presence registry rather than from the database, so the
    keepalive only touches rounds that are actually live here. In a multi-node
    deployment each node heartbeats the rounds it holds, which is enough: any
    one of them refreshing the group keeps every member of it alive.
    """
    found: set[int] = set()
    for group_name in LOCAL_MEMBERS:
        parts = group_name.split(".")
        if len(parts) < 2:
            continue
        try:
            found.add(int(parts[1]))
        except ValueError:
            continue
    return found


async def broadcast_once(channel_layer, competition_ids=None) -> int:
    """Send one ``tick`` per round. Returns how many rounds were reached.

    Split out from the loop so a test can drive a single heartbeat without
    waiting for the interval.
    """
    ids = live_competition_ids() if competition_ids is None else set(competition_ids)

    sent = 0
    for competition_id in sorted(ids):
        payload = await _tick_payload(competition_id)
        if payload is None:
            continue

        message = {"type": "tick", "payload": payload}
        await channel_layer.group_send(groups.screen_group(competition_id), message)
        await channel_layer.group_send(groups.teacher_group(competition_id), message)
        sent += 1

    return sent


@database_sync_to_async
def _tick_payload(competition_id: int) -> dict | None:
    """Read the clock for one round, in a worker thread."""
    from django.utils import timezone

    from competitions.models import Competition
    from live.clock import remaining_seconds

    competition = (
        Competition.objects.filter(pk=competition_id)
        .select_related("current_question")
        .first()
    )
    if competition is None:
        return None

    now = timezone.now()
    return {
        "competition_id": competition_id,
        "server_time": now.isoformat(),
        "seconds_remaining": round(
            remaining_seconds(competition.current_question_ends_at, now), 3
        ),
        "state": competition.state,
        "question_number": competition.question_number,
    }


async def _run(channel_layer) -> None:
    """Broadcast a keepalive to every attached round, until cancelled."""
    interval = heartbeat_interval()

    try:
        while True:
            await asyncio.sleep(interval)

            try:
                await broadcast_once(channel_layer)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # A keepalive failure must never take the web process down. The
                # next tick tries again, and the group expiry setting is the
                # backstop that keeps memberships alive meanwhile.
                logger.exception("Live heartbeat failed; will retry next interval.")
    except asyncio.CancelledError:
        pass


__all__ = [
    "broadcast_once",
    "ensure_running",
    "heartbeat_interval",
    "is_running",
    "live_competition_ids",
    "stop",
]
