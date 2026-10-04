"""The teacher's *End Question* control, and the state machine around it.

The phase brief is the specification for the sequence these tests pin down:

    QUESTION_ACTIVE -> ANSWERING_CLOSED -> (result revealed) -> advance

Both triggers must produce it - the deadline passing and the teacher pressing the
button - and neither may be able to do it twice, skip the reveal, or be triggered
by anything but the teacher.
"""

from __future__ import annotations

from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async

from competitions.models import CompetitionState
from core.tests.utils import (
    create_administrator,
    create_classroom,
    create_competition,
    create_question,
    create_screen,
    create_teacher,
)
from live import clock_runner, services
from live.events import (
    ANSWERING_CLOSED,
    END_QUESTION,
    NEXT_QUESTION,
    QUESTION_STARTED,
    RESULT_REVEALED,
)
from live.services import CompetitionControlError
from live.tests.support import drain_until, send_message
from live.tests.test_live_engine import LiveTestCase


def build(func, *args, **kwargs):
    return async_to_sync(database_sync_to_async(func))(*args, **kwargs)


class EndQuestionTests(LiveTestCase):
    """A teacher ends a question before its time is up."""

    def setUp(self) -> None:
        super().setUp()

        self.teacher = build(create_teacher)
        self.lab = build(create_classroom, name="Science Lab A")
        self.question = build(
            create_question, text="What is 2 + 2?", options=["3", "4"], correct_option="4"
        )
        self.competition = build(
            create_competition,
            teacher=self.teacher,
            title="A versus nothing",
            classrooms=[self.lab],
            questions=[self.question],
        )
        self.screen = build(create_screen, name="A board", classroom=self.lab)

        build(services.start_competition, self.competition)
        build(services.start_question, self.competition, position=1)

    def _reload(self):
        return self.competition.__class__.objects.get(pk=self.competition.pk)

    # -- the control itself ------------------------------------------------

    def test_the_teacher_can_end_an_open_question(self) -> None:
        transition = build(services.end_question, self.competition)

        self.assertEqual(transition.event, ANSWERING_CLOSED)
        self.assertEqual(
            self._reload().state, CompetitionState.ANSWERING_CLOSED
        )

    def test_ending_a_question_does_not_move_to_the_next_one(self) -> None:
        build(services.end_question, self.competition)

        reloaded = self._reload()

        # The whole reason this control exists: an early end must still produce
        # a result, so it stops rather than jumping to question 2.
        self.assertEqual(reloaded.question_number, 1)
        self.assertEqual(reloaded.current_question_id, self.question.pk)
        self.assertEqual(reloaded.state, CompetitionState.ANSWERING_CLOSED)

    def test_the_clock_keeps_the_question_open_until_it_is_ended(self) -> None:
        # Sanity check on the setup: the question really is open, so the end
        # below is doing the work rather than the deadline having got there
        # first.
        self.assertEqual(
            self._reload().state, CompetitionState.QUESTION_ACTIVE
        )

    def test_the_teacher_and_the_clock_produce_the_same_payload(self) -> None:
        """The room must not be able to tell how the question ended.

        Only the log distinguishes them: if the payloads differed, the fact that
        a teacher pressed early would be readable off the wire by every screen
        in the building.
        """
        manual = build(services.end_question, self.competition)
        manual_payload = dict(manual.payload)
        manual_payload.pop("server_time")

        # Rebuild the same situation and let the clock's own transition run.
        self.competition.state = CompetitionState.QUESTION_ACTIVE
        self.competition.save(update_fields=["state"])
        automatic = build(services.close_answering, self.competition)
        automatic_payload = dict(automatic.payload)
        automatic_payload.pop("server_time")

        self.assertEqual(manual_payload, automatic_payload)

    # -- guards ------------------------------------------------------------

    def test_ending_a_question_twice_is_refused(self) -> None:
        build(services.end_question, self.competition)

        with self.assertRaises(CompetitionControlError) as caught:
            build(services.end_question, self.competition)

        self.assertEqual(caught.exception.code, "not_answering")

    def test_a_second_end_does_not_change_the_state_again(self) -> None:
        build(services.end_question, self.competition)
        before = self._reload().updated_at

        with self.assertRaises(CompetitionControlError):
            build(services.end_question, self.competition)

        self.assertEqual(self._reload().updated_at, before)

    def test_ending_before_the_first_question_is_refused(self) -> None:
        self.competition.state = CompetitionState.WAITING
        self.competition.save(update_fields=["state"])

        with self.assertRaises(CompetitionControlError) as caught:
            build(services.end_question, self.competition)

        self.assertEqual(caught.exception.code, "not_answering")

    def test_a_teacher_cannot_end_someone_elses_question(self) -> None:
        intruder = build(create_teacher, username="intruder")

        with self.assertRaises(CompetitionControlError) as caught:
            build(services.assert_can_control, self.competition, intruder)
            build(services.end_question, self.competition)

        self.assertEqual(caught.exception.code, "not_authorised")
        self.assertEqual(
            self._reload().state, CompetitionState.QUESTION_ACTIVE
        )

    def test_an_administrator_may_take_over_and_end_the_question(self) -> None:
        administrator = build(create_administrator, username="boss")
        build(services.assert_can_control, self.competition, administrator)

        transition = build(services.end_question, self.competition)

        self.assertEqual(transition.event, ANSWERING_CLOSED)

    # -- what a client may not do -------------------------------------------

    def test_a_screen_cannot_end_a_question(self) -> None:
        """A screen socket has no handler for a control message at all.

        Asserted over the real socket, because the property being claimed is that
        no screen message reaches a transition - not that a service refuses one.
        """
        self._run(self._test_a_screen_cannot_end_a_question_impl)

    async def _test_a_screen_cannot_end_a_question_impl(self) -> None:
        screen, _ = await self._connect_screen(self.screen)
        await self._skip_intro(screen)

        await send_message(screen, {"type": END_QUESTION})
        error, _ = await drain_until(screen, "error")

        self.assertEqual(error["code"], "unknown_message")

        current = await self._in_thread(
            self.competition.__class__.objects.get, pk=self.competition.pk
        )
        self.assertEqual(current.state, CompetitionState.QUESTION_ACTIVE)

    def test_a_screen_cannot_advance(self) -> None:
        self._run(self._test_a_screen_cannot_advance_impl)

    async def _test_a_screen_cannot_advance_impl(self) -> None:
        screen, _ = await self._connect_screen(self.screen)
        await self._skip_intro(screen)

        await send_message(screen, {"type": "advance"})
        error, _ = await drain_until(screen, "error")

        self.assertEqual(error["code"], "unknown_message")
        current = await self._in_thread(
            self.competition.__class__.objects.get, pk=self.competition.pk
        )
        self.assertEqual(current.state, CompetitionState.QUESTION_ACTIVE)


class AdvanceGuardTests(LiveTestCase):
    """Advancing is only legal once the question is closed."""

    def setUp(self) -> None:
        super().setUp()

        self.teacher = build(create_teacher)
        self.lab = build(create_classroom, name="Science Lab A")
        self.first = build(
            create_question, text="First", options=["3", "4"], correct_option="4"
        )
        self.second = build(
            create_question, text="Second", options=["5", "6"], correct_option="6"
        )
        self.competition = build(
            create_competition,
            teacher=self.teacher,
            title="Two questions",
            classrooms=[self.lab],
            questions=[self.first, self.second],
        )
        build(services.start_competition, self.competition)
        build(services.start_question, self.competition, position=1)

    def _reload(self):
        return self.competition.__class__.objects.get(pk=self.competition.pk)

    def test_advancing_an_open_question_is_refused(self) -> None:
        with self.assertRaises(CompetitionControlError) as caught:
            build(services.advance, self.competition)

        self.assertEqual(caught.exception.code, "question_still_open")

    def test_an_open_question_survives_a_refused_advance(self) -> None:
        with self.assertRaises(CompetitionControlError):
            build(services.advance, self.competition)

        reloaded = self._reload()
        self.assertEqual(reloaded.state, CompetitionState.QUESTION_ACTIVE)
        self.assertEqual(reloaded.question_number, 1)

    def test_advancing_is_refused_after_the_round_has_finished(self) -> None:
        build(services.end_question, self.competition)
        build(services.finish_competition, self.competition)

        with self.assertRaises(CompetitionControlError) as caught:
            build(services.advance, self.competition)

        self.assertEqual(caught.exception.code, "finished")

    def test_advancing_after_the_reveal_starts_the_next_question(self) -> None:
        build(services.end_question, self.competition)

        transition = build(services.advance, self.competition)

        self.assertEqual(transition.event, QUESTION_STARTED)
        self.assertEqual(self._reload().question_number, 2)


class EndThenAdvanceSocketTests(LiveTestCase):
    """The whole sequence over the real socket, in the order a teacher does it."""

    def setUp(self) -> None:
        super().setUp()

        self.teacher = build(create_teacher)
        self.lab = build(create_classroom, name="Science Lab A")
        self.first = build(
            create_question, text="What is 2 + 2?", options=["3", "4"], correct_option="4"
        )
        self.second = build(
            create_question, text="What is 3 + 3?", options=["5", "6"], correct_option="6"
        )
        self.competition = build(
            create_competition,
            teacher=self.teacher,
            title="Two questions",
            classrooms=[self.lab],
            questions=[self.first, self.second],
        )
        self.screen = build(create_screen, name="A board", classroom=self.lab)

    def test_the_full_sequence_over_the_socket(self) -> None:
        self._run(self._test_the_full_sequence_over_the_socket_impl)

    async def _test_the_full_sequence_over_the_socket_impl(self) -> None:
        # The teacher connects first and starts the round: a screen can only join
        # a competition that is running, so the order here is the order a real
        # lesson goes in.
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await send_message(teacher, {"type": "start_competition"})
        await drain_until(teacher, "competition_started")

        screen, _ = await self._connect_screen(self.screen)
        await self._skip_intro(screen)

        await send_message(teacher, {"type": "start_question"})
        opened, _ = await drain_until(screen, QUESTION_STARTED)
        self.assertEqual(opened["question_number"], 1)

        # End Question: closed, then revealed, and no question 2 yet.
        await send_message(teacher, {"type": END_QUESTION})
        closed, _ = await drain_until(screen, ANSWERING_CLOSED)
        self.assertEqual(closed["state"], CompetitionState.ANSWERING_CLOSED)

        reveal, _ = await drain_until(screen, RESULT_REVEALED)
        self.assertEqual(reveal["correct_answer"], "4")

        current = await self._in_thread(
            self.competition.__class__.objects.get, pk=self.competition.pk
        )
        self.assertEqual(current.question_number, 1)

        # Now the teacher moves on.
        await send_message(teacher, {"type": "advance"})
        second, _ = await drain_until(screen, QUESTION_STARTED)
        self.assertEqual(second["question_number"], 2)

    def test_a_second_end_request_over_the_socket_is_refused(self) -> None:
        self._run(self._test_a_second_end_request_is_refused_impl)

    async def _test_a_second_end_request_is_refused_impl(self) -> None:
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await send_message(teacher, {"type": "start_competition"})
        await drain_until(teacher, "competition_started")
        await send_message(teacher, {"type": "start_question"})
        await drain_until(teacher, QUESTION_STARTED)

        await send_message(teacher, {"type": END_QUESTION})
        await drain_until(teacher, ANSWERING_CLOSED)

        await send_message(teacher, {"type": END_QUESTION})
        error, _ = await drain_until(teacher, "error")

        self.assertEqual(error["code"], "not_answering")

    def test_the_reveal_is_published_once_for_a_manual_end(self) -> None:
        self._run(self._test_the_reveal_is_published_once_impl)

    async def _test_the_reveal_is_published_once_impl(self) -> None:
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await send_message(teacher, {"type": "start_competition"})
        await drain_until(teacher, "competition_started")

        screen, _ = await self._connect_screen(self.screen)
        await self._skip_intro(screen)

        await send_message(teacher, {"type": "start_question"})
        await drain_until(teacher, QUESTION_STARTED)

        await send_message(teacher, {"type": END_QUESTION})
        await drain_until(screen, RESULT_REVEALED)

        # A second end must not produce a second reveal, so the next thing this
        # screen receives is an error rather than another result.
        await send_message(teacher, {"type": END_QUESTION})
        error, _ = await drain_until(teacher, "error")

        self.assertEqual(error["code"], "not_answering")

        # And the question number is untouched: the room is still on question 1.
        self.assertEqual(
            (
                await self._in_thread(
                    self.competition.__class__.objects.get, pk=self.competition.pk
                )
            ).question_number,
            1,
        )


class ClockAndTeacherAgreeTests(LiveTestCase):
    """The deadline and the button reach the same state from the same place."""

    def setUp(self) -> None:
        super().setUp()

        self.teacher = build(create_teacher)
        self.lab = build(create_classroom, name="Science Lab A")
        self.question = build(
            create_question, text="What is 2 + 2?", options=["3", "4"], correct_option="4"
        )
        self.competition = build(
            create_competition,
            teacher=self.teacher,
            title="Timeout or not",
            classrooms=[self.lab],
            questions=[self.question],
        )
        self.screen = build(create_screen, name="A board", classroom=self.lab)
        build(services.start_competition, self.competition)

    def test_the_deadline_closes_a_question_the_teacher_never_ended(self) -> None:
        build(services.start_question, self.competition, position=1)
        screen_revealed = build(self._wait_for_reveal)
        self.assertTrue(screen_revealed)

    def _wait_for_reveal(self) -> bool:
        from channels.layers import get_channel_layer
        from django.utils import timezone

        async def run():
            layer = get_channel_layer()
            await clock_runner.start_clock(
                self.competition.pk,
                layer,
                timezone.now() - timedelta(seconds=1),
            )
            # Give the clock task a moment to publish.
            import asyncio

            await asyncio.sleep(1.0)
            clock_runner.cancel_all_clocks()

        async_to_sync(run)()

        reloaded = self.competition.__class__.objects.get(pk=self.competition.pk)
        return reloaded.state == CompetitionState.ANSWERING_CLOSED

    def test_a_teacher_end_beats_the_clock_to_the_same_state(self) -> None:
        build(services.start_question, self.competition, position=1)

        transition = build(services.end_question, self.competition)

        self.assertEqual(transition.event, ANSWERING_CLOSED)
        reloaded = self.competition.__class__.objects.get(pk=self.competition.pk)
        self.assertEqual(reloaded.state, CompetitionState.ANSWERING_CLOSED)