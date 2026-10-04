"""The server-side competition clock.

Why a background task instead of a browser timer
------------------------------------------------
A screen's countdown must be the same number everywhere, and it must not depend
on any browser being awake. So the deadline is enforced here:

* when a question starts, the server records an absolute ``ends_at``;
* this task sleeps until that instant and then publishes ``answering_closed``;
* every screen renders its countdown from ``ends_at``, corrected by the offset
  measured against the server clock.

Because ``answering_closed`` is broadcast rather than computed per client, a
screen that was asleep, throttled or briefly disconnected still lands on the
right state - it simply receives the event late.

The task is started by the teacher consumer when a question begins, and it is
cancelled if the teacher moves on first. Only one runs per competition, and it
is keyed by competition id so a reconnecting teacher does not start a second.
"""

from __future__ import annotations

import asyncio
import logging

from channels.db import database_sync_to_async

logger = logging.getLogger(__name__)

# Running tasks by competition id, so a second trigger cannot start a duplicate
# clock for a round that already has one.
_CLOCK_TASKS: dict[int, asyncio.Task] = {}

# How often the clock wakes to check whether it has been superseded. Short
# enough that cancelling a question feels immediate, long enough to be free.
POLL_INTERVAL_SECONDS = 0.5


def cancel_clock(competition_id: int) -> None:
    """Stop the clock for a competition, if one is running."""
    task = _CLOCK_TASKS.pop(competition_id, None)
    if task is not None and not task.done():
        task.cancel()


def clock_running(competition_id: int) -> bool:
    task = _CLOCK_TASKS.get(competition_id)
    return task is not None and not task.done()


async def start_clock(competition_id: int, channel_layer, ends_at) -> None:
    """Ensure exactly one clock is running for this competition.

    Called whenever a question starts. If a clock is already running for the
    round it is cancelled and replaced, so that starting question 2 never leaves
    question 1's deadline able to close question 2 early.
    """
    cancel_clock(competition_id)

    if ends_at is None:
        return

    _CLOCK_TASKS[competition_id] = asyncio.create_task(
        _run(competition_id, channel_layer, ends_at)
    )


async def _run(competition_id: int, channel_layer, ends_at) -> None:
    """Wait until the deadline, then broadcast ``answering_closed``.

    The wait is polled in short slices rather than slept through in one call so
    that a cancelled question stops promptly instead of after the full duration.
    """
    from django.utils import timezone

    from live import groups

    try:
        while True:
            if _CLOCK_TASKS.get(competition_id) is not asyncio.current_task():
                # Superseded by a newer clock for this round.
                return

            remaining = (ends_at - timezone.now()).total_seconds()
            if remaining <= 0:
                break

            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))

        transition = await _close_answering(competition_id)
        if transition is None:
            return

        payload = {"type": transition.event, "payload": transition.payload}
        await channel_layer.group_send(
            groups.screen_group(competition_id), payload
        )
        await channel_layer.group_send(
            groups.teacher_group(competition_id), payload
        )

        # Answering is now closed on the server's own initiative, so the outcome
        # may be shown. This is the same reveal the teacher consumer publishes
        # when a teacher closes the question early - the classroom must not be
        # able to tell which of the two ended it.
        from scoring import realtime

        await realtime.broadcast_result_revealed(channel_layer, competition_id)

        logger.info(
            "Competition %s closed answering on the server clock.", competition_id
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        # A clock failure must be visible in the logs but must not take down the
        # web process. Screens re-sync from the database on their next state_sync.
        logger.exception(
            "Competition clock failed for competition %s.", competition_id
        )


@database_sync_to_async
def _close_answering(competition_id: int):
    """Load the competition and close answering if it is genuinely still open.

    The state check matters: by the time the deadline arrives the teacher may
    already have advanced, and closing a question that is no longer active would
    broadcast a stale ``answering_closed`` over the top of the next question.
    """
    from competitions.models import Competition, CompetitionState
    from live import services as competition_services

    competition = Competition.objects.filter(pk=competition_id).first()
    if competition is None:
        return None
    if competition.state != CompetitionState.QUESTION_ACTIVE:
        return None
    try:
        return competition_services.close_answering(competition)
    except competition_services.CompetitionControlError:
        return None


def cancel_all_clocks() -> None:
    """Stop every running clock. Used between tests and on shutdown."""
    for competition_id in list(_CLOCK_TASKS):
        cancel_clock(competition_id)
