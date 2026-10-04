"""Every group broadcast must have a handler on the consumer that receives it.

Channels dispatches a group message to the method named after its ``type`` and
raises ``ValueError: No handler for message type ...`` if the consumer has no such
method. That exception is raised inside the consumer, so the socket is torn down
rather than the message being dropped: the client sees a clean close and every
later message it sends is silently ignored.

This is not hypothetical. ``result_revealed``, ``leaderboard_updated`` and
``competition_results`` are all broadcast to the teacher group, and
``TeacherConsumer`` implemented none of them - so the teacher's dashboard was
disconnected by the first reveal of the first question and never recovered.
Screens and the leaderboard display were unaffected, which is why a round driven
from a screen still looked correct.

Two tests guard against that:

* the table test below, which fails at review time when a broadcast is added
  without a handler;
* a full round driven through a real teacher socket, which fails for any missing
  handler regardless of whether the table was updated.
"""

from __future__ import annotations

from live import events, groups
from live.consumers import ScreenConsumer
from live.leaderboard_consumer import LeaderboardConsumer
from live.teacher_consumer import TeacherConsumer
from live.tests.support import drain_until, send_message
from live.tests.test_live_engine import LiveTestCase
from scoring import realtime

# The Channels envelope type for a presence update. Deliberately not
# `events.PRESENCE`: that is the name on the wire the teacher sees, while the
# method name has to match the envelope. Getting the two confused is exactly how
# a handler ends up in the wrong place.
PRESENCE_CHANGED = "presence_changed"

# Transition events, broadcast to screens and the teacher by
# `TeacherConsumer._apply` and by the clock runner.
TRANSITIONS = (
    events.COMPETITION_STARTED,
    events.QUESTION_STARTED,
    events.ANSWERING_CLOSED,
    events.NEXT_QUESTION,
    events.COMPETITION_FINISHED,
)

# Sent to every group by `scoring.realtime._broadcast_all`, so any consumer that
# joins one of them must be able to render all three.
SCORING_RELEASES = (
    events.RESULT_REVEALED,
    events.LEADERBOARD_UPDATED,
    events.COMPETITION_RESULTS,
)

# The message types that can arrive on each group.
#
# This is one-directional on purpose: a group message with no handler closes the
# socket, so every entry here must have a method - but a consumer holding extra
# handlers is harmless and in fact necessary as insurance. Screens ignore
# `answer_progress` on other parties' behalf, and only the teacher group is told
# how many screens are attached.
GROUP_MESSAGE_TYPES = {
    groups.screen_group: TRANSITIONS
    + SCORING_RELEASES
    + (events.ANSWER_PROGRESS, events.TICK),
    groups.teacher_group: TRANSITIONS
    + SCORING_RELEASES
    + (PRESENCE_CHANGED, events.TICK),
    groups.leaderboard_group: SCORING_RELEASES + (events.TICK,),
}

GROUP_OWNERS = {
    groups.screen_group: ScreenConsumer,
    groups.teacher_group: TeacherConsumer,
    groups.leaderboard_group: LeaderboardConsumer,
}


class BroadcastCoverageTests(LiveTestCase):
    def test_every_group_message_has_a_handler_on_every_consumer(self) -> None:
        missing = []
        for build_group, consumer in GROUP_OWNERS.items():
            for message_type in GROUP_MESSAGE_TYPES[build_group]:
                if not callable(getattr(consumer, message_type, None)):
                    missing.append(f"{consumer.__name__}.{message_type}")

        self.assertEqual(
            missing,
            [],
            "These group messages have no handler, so the socket receiving them "
            "would be closed by Channels rather than shown the result: "
            + ", ".join(missing),
        )

    def test_the_table_names_a_handler_for_every_group(self) -> None:
        """Guards the guard: an unmapped group would pass the test above."""
        self.assertEqual(
            sorted(id(builder) for builder in GROUP_OWNERS),
            sorted(id(builder) for builder in GROUP_MESSAGE_TYPES),
        )


class TeacherSurvivesTheRoundTests(LiveTestCase):
    """The teacher's socket must stay open for the whole round.

    Every existing reveal test watches from a screen. This one watches from the
    teacher, which is the only party whose survival was not covered.
    """

    async def test_the_teacher_receives_the_reveal_and_stays_connected(self) -> None:
        teacher = await self._in_thread(self._teacher)
        lab = await self._in_thread(self._lab)
        competition = await self._in_thread(
            self._competition, teacher=teacher, lab=lab
        )

        communicator, connected = await self._connect_teacher(teacher, competition)
        self.assertTrue(connected, "the teacher socket was not accepted")
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": events.START_COMPETITION})
        await drain_until(communicator, events.COMPETITION_STARTED)

        await send_message(communicator, {"type": events.START_QUESTION})
        await drain_until(communicator, events.QUESTION_STARTED)

        # Closing answering publishes the reveal to every group, the teacher
        # group included. This is the message the teacher had no handler for.
        await send_message(communicator, {"type": events.END_QUESTION})
        reveal, _ = await drain_until(communicator, events.RESULT_REVEALED)

        # The key may be shown to the teacher, and the standings with it.
        self.assertIn("correct_answer", reveal)
        self.assertIn("leaderboard", reveal)

        # The socket is still usable: a dropped connection would show up here as
        # a timeout with no message, rather than passing silently.
        await send_message(communicator, {"type": events.FINISH_COMPETITION})
        results, _ = await drain_until(communicator, events.COMPETITION_RESULTS)

        self.assertEqual(results["competition_id"], competition.pk)

        await self._disconnect(communicator)

    # -- fixtures ------------------------------------------------------

    def _teacher(self):
        from core.tests.utils import create_teacher

        return create_teacher()

    def _lab(self):
        from core.tests.utils import create_classroom

        return create_classroom(name="Science Lab A")

    def _competition(self, *, teacher, lab):
        from core.tests.utils import create_competition, create_question

        question = create_question(
            text="What is 2 + 2?", options=["3", "4"], correct_option="4"
        )
        return create_competition(
            teacher=teacher,
            title="A full round",
            classrooms=[lab],
            questions=[question],
        )


class ScoringBroadcastReachesEachOwnerTests(LiveTestCase):
    """The scoring broadcasts must actually target all three groups.

    The coverage table asserts the handlers exist; this asserts the broadcasts
    are aimed at the groups whose consumers handle them, so a typo in a group
    name cannot leave the teacher waiting for a reveal that goes only to screens.
    """

    async def test_the_reveal_reaches_the_teacher_group(self) -> None:
        sent = []

        class RecordingLayer:
            async def group_send(self, group, message):
                sent.append((group, message["type"]))

            # The layer is used as a context manager by some code paths.
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        teacher = await self._in_thread(self._teacher)
        lab = await self._in_thread(self._lab)
        competition = await self._in_thread(
            self._competition, teacher=teacher, lab=lab
        )
        await self._in_thread(_start, competition)

        await realtime.broadcast_result_revealed(RecordingLayer(), competition.pk)

        groups_sent = {group for group, _ in sent}
        self.assertIn(groups.teacher_group(competition.pk), groups_sent)
        self.assertIn(groups.screen_group(competition.pk), groups_sent)
        self.assertIn(groups.leaderboard_group(competition.pk), groups_sent)
        self.assertTrue(
            all(event_type == events.RESULT_REVEALED for _, event_type in sent),
            f"unexpected event types sent: {sorted({t for _, t in sent})}",
        )

    def _teacher(self):
        from core.tests.utils import create_teacher

        return create_teacher()

    def _lab(self):
        from core.tests.utils import create_classroom

        return create_classroom(name="Science Lab A")

    def _competition(self, *, teacher, lab):
        from core.tests.utils import create_competition, create_question

        question = create_question(
            text="What is 2 + 2?", options=["3", "4"], correct_option="4"
        )
        return create_competition(
            teacher=teacher,
            title="Broadcast targets",
            classrooms=[lab],
            questions=[question],
        )


def _start(competition) -> None:
    from live import services

    services.start_competition(competition)
    services.start_question(competition, position=1)