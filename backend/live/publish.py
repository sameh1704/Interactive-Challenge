"""Publishing committed transitions to the channel layer.

Shared by the teacher WebSocket consumer and the HTTP start view so that a
round started by pressing the "Start Competition" button on a teacher page
delivers the same ``competition_started`` events to the same screen groups as a
start that originates from the live teacher dashboard. Only one protocol, one
place, one test target.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync

from live import events, groups


def _publish_transition_sync(layer, transition) -> None:
    """Synchronous broadcast of a transition.

    Mirrors :meth:`live.teacher_consumer.TeacherLiveConsumer._apply`, without
    the per-connection state (clock sync, state_sync and presence): those are
    specific to a connected consumer session. The screen groups and the waiting
    group must both receive the start event so already-open screens reconnect
    and newly-connected screens learn the running competition.
    """
    if transition is None:
        return

    competition_id = transition.competition.pk
    payload = {
        "type": transition.event,
        **transition.payload,
    }

    # Broadcast to the competition's screen group and teacher group. The
    # teacher group is here for symmetry; HTTP-started rounds may not have a
    # teacher socket open, but a live dashboard that connects later receives
    # events through the consumer's ``competition_started`` handler.
    async_to_sync(layer.group_send)(
        groups.screen_group(competition_id), {"type": transition.event, "payload": payload}
    )
    async_to_sync(layer.group_send)(
        groups.teacher_group(competition_id), {"type": transition.event, "payload": payload}
    )

    if transition.event == events.COMPETITION_STARTED:
        # Nudge the screens that were waiting, so the classrooms this round
        # named join it without their sockets having to reconnect to find out.
        async_to_sync(layer.group_send)(
            groups.waiting_group(),
            {"type": transition.event, "payload": payload},
        )


async def publish_transition(layer, transition) -> None:
    """Asynchronous broadcast of a transition, for use inside async contexts."""
    if transition is None:
        return

    competition_id = transition.competition.pk
    payload = {
        "type": transition.event,
        **transition.payload,
    }

    await layer.group_send(
        groups.screen_group(competition_id), {"type": transition.event, "payload": payload}
    )
    await layer.group_send(
        groups.teacher_group(competition_id), {"type": transition.event, "payload": payload}
    )

    if transition.event == events.COMPETITION_STARTED:
        await layer.group_send(
            groups.waiting_group(),
            {"type": transition.event, "payload": payload},
        )


publish_transition_sync = _publish_transition_sync
