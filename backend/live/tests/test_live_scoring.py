"""Answering over the live WebSocket, and the reveal that follows.

:mod:`scoring.services` is tested without a socket in ``scoring.tests``. This
module covers the part only a socket can show: that a screen's message is routed
to the right classroom's answer, that the acknowledgement tells the submitter
nothing it should not know, and that the answer key stays hidden until answering
closes - whichever party closed it.

It reuses :class:`live.tests.test_live_engine.LiveTestCase` for the socket
harness rather than repeating it: an in-memory channel layer, communicators torn
down in the loop that opened them, and a signed-in teacher scope.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from config.asgi import application
from core.tests.utils import (
    create_classroom,
    create_competition,
    create_question,
    create_screen,
    create_teacher,
)
from live import clock_runner, services
from live.events import (
    ANSWER_PROGRESS,
    COMPETITION_RESULTS,
    RESULT_REVEALED,
    STATE_SYNC,
    SUBMIT_ANSWER,
    TICK,
)
from live.tests.support import (
    CONNECT_TIMEOUT_SECONDS,
    drain_until,
    origin_headers,
    send_message,
)
from live.tests.test_live_engine import LiveTestCase


def build(func, *args, **kwargs):
    """Run a factory in a worker thread, from a synchronous ``setUp``.

    ``LiveTestCase._in_thread`` is a coroutine, which suits an async test body
    but not setup. This is the synchronous door to the same machinery.
    """
    return async_to_sync(database_sync_to_async(func))(*args, **kwargs)


def channel_layer():
    from channels.layers import get_channel_layer

    return get_channel_layer()


def stored_answers(competition_id: int):
    from scoring.models import Answer

    return list(Answer.objects.for_competition(competition_id))


def stored_result(competition_id: int):
    from scoring.models import CompetitionResult

    return CompetitionResult.objects.filter(competition_id=competition_id).first()


class AnswerSubmissionSocketTests(LiveTestCase):
    """A screen submits an answer; the server decides what it scores."""

    def setUp(self) -> None:
        super().setUp()

        self.teacher = build(create_teacher)
        self.lab_a = build(create_classroom, name="Science Lab A")
        self.lab_b = build(create_classroom, name="Science Lab B")
        self.question = build(
            create_question,
            text="What is 2 + 2?",
            options=["3", "4", "5", "6"],
            correct_option="4",
        )
        self.competition = build(
            create_competition,
            teacher=self.teacher,
            title="A versus B",
            classrooms=[self.lab_a, self.lab_b],
            questions=[self.question],
        )
        self.screen_a = build(create_screen, name="A board", classroom=self.lab_a)
        self.screen_b = build(create_screen, name="B board", classroom=self.lab_b)

        build(services.start_competition, self.competition)

    def _start_question(self):
        build(services.start_question, self.competition, position=1)

    # -- acceptance --------------------------------------------------------

    def test_a_screen_can_submit_an_answer(self) -> None:
        self._start_question()
        self._run(self._test_a_screen_can_submit_an_answer_impl)

    async def _test_a_screen_can_submit_an_answer_impl(self) -> None:
        communicator, connected = await self._connect_screen(self.screen_a)
        self.assertTrue(connected)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": "4"})
        ack, _ = await drain_until(communicator, "answer_accepted")

        self.assertEqual(ack["position"], 1)

        answers = await self._in_thread(stored_answers, self.competition.pk)
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].classroom_id, self.lab_a.pk)
        self.assertTrue(answers[0].is_correct)

    def test_the_acknowledgement_reveals_nothing_to_the_submitter(self) -> None:
        """No correctness, no score - not even to the class that answered.

        Telling one screen it was right is how every other screen finds out, so
        the acknowledgement carries neither.
        """
        self._start_question()
        self._run(self._test_the_acknowledgement_reveals_nothing_impl)

    async def _test_the_acknowledgement_reveals_nothing_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": "4"})
        ack, _ = await drain_until(communicator, "answer_accepted")

        self.assertNotIn("is_correct", ack)
        self.assertNotIn("correct_answer", ack)
        self.assertNotIn("score", ack)
        self.assertNotIn("points", ack)

    def test_a_wrong_answer_is_acknowledged_without_scoring(self) -> None:
        self._start_question()
        self._run(self._test_a_wrong_answer_is_acknowledged_without_scoring_impl)

    async def _test_a_wrong_answer_is_acknowledged_without_scoring_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": "5"})
        await drain_until(communicator, "answer_accepted")

        answers = await self._in_thread(stored_answers, self.competition.pk)
        self.assertFalse(answers[0].is_correct)
        self.assertEqual(answers[0].total_score, 0)

    # -- refusals ----------------------------------------------------------

    def test_a_second_answer_from_the_same_screen_is_refused(self) -> None:
        self._start_question()
        self._run(self._test_a_second_answer_is_refused_impl)

    async def _test_a_second_answer_is_refused_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": "4"})
        await drain_until(communicator, "answer_accepted")

        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": "5"})
        error, _ = await drain_until(communicator, "error")

        self.assertEqual(error["code"], "already_answered")
        answers = await self._in_thread(stored_answers, self.competition.pk)
        self.assertEqual(len(answers), 1)

    def test_an_answer_after_the_deadline_is_refused(self) -> None:
        self._start_question()
        self._run(self._test_an_answer_after_the_deadline_is_refused_impl)

    async def _test_an_answer_after_the_deadline_is_refused_impl(self) -> None:
        await self._in_thread(self._force_deadline_into_the_past, 5)

        communicator, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": "4"})
        error, _ = await drain_until(communicator, "error")

        self.assertEqual(error["code"], "late_answer")
        self.assertEqual(
            await self._in_thread(stored_answers, self.competition.pk), []
        )

    def _force_deadline_into_the_past(self, seconds: int) -> None:
        from competitions.models import Competition

        Competition.objects.filter(pk=self.competition.pk).update(
            current_question_ends_at=timezone.now() - timedelta(seconds=seconds)
        )

    def test_an_answer_with_no_option_is_refused(self) -> None:
        self._start_question()
        self._run(self._test_an_answer_with_no_option_is_refused_impl)

    async def _test_an_answer_with_no_option_is_refused_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": SUBMIT_ANSWER})
        error, _ = await drain_until(communicator, "error")

        self.assertEqual(error["code"], "missing_answer")

    def test_an_option_that_does_not_exist_is_refused(self) -> None:
        self._start_question()
        self._run(self._test_an_option_that_does_not_exist_is_refused_impl)

    async def _test_an_option_that_does_not_exist_is_refused_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": "42"})
        error, _ = await drain_until(communicator, "error")

        self.assertEqual(error["code"], "invalid_answer")

    # -- client manipulation ------------------------------------------------

    def test_a_screen_cannot_score_itself(self) -> None:
        """Every field a client might add to inflate its own score is ignored."""
        self._start_question()
        self._run(self._test_a_screen_cannot_score_itself_impl)

    async def _test_a_screen_cannot_score_itself_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(communicator)

        await send_message(
            communicator,
            {
                "type": SUBMIT_ANSWER,
                "answer": "5",  # wrong, on purpose
                "is_correct": True,
                "score": 10000,
                "points": 10000,
                "speed_bonus": 5000,
                "response_time": 0,
                "response_time_seconds": 0,
                "classroom_id": self.lab_b.pk,
            },
        )
        await drain_until(communicator, "answer_accepted")

        answers = await self._in_thread(stored_answers, self.competition.pk)
        self.assertEqual(len(answers), 1)
        self.assertFalse(answers[0].is_correct)
        self.assertEqual(answers[0].total_score, 0)
        # And it scored for its own classroom, not the one it nominated.
        self.assertEqual(answers[0].classroom_id, self.lab_a.pk)

    def test_a_screen_cannot_answer_for_another_classroom(self) -> None:
        """Lab A's board cannot record an answer for Lab B."""
        self._start_question()
        self._run(self._test_a_screen_cannot_answer_for_another_classroom_impl)

    async def _test_a_screen_cannot_answer_for_another_classroom_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(communicator)

        await send_message(
            communicator,
            {
                "type": SUBMIT_ANSWER,
                "answer": "4",
                "classroom_id": self.lab_b.pk,
                "screen_id": self.screen_b.screen_id,
            },
        )
        await drain_until(communicator, "answer_accepted")

        answers = await self._in_thread(stored_answers, self.competition.pk)
        self.assertEqual(answers[0].classroom_id, self.lab_a.pk)
        self.assertEqual(answers[0].screen_id, self.screen_a.pk)

    def test_an_unidentified_screen_cannot_answer(self) -> None:
        """A socket with no screen identity never gets a connection at all.

        The screen is who you are, and without it there is no classroom to
        answer for. Refusing the handshake is the only honest answer: an
        accepted socket with no identity would just be a socket whose every
        later message has to be guessed at.
        """
        self._start_question()
        self._run(self._test_an_unidentified_screen_cannot_answer_impl)

    async def _test_an_unidentified_screen_cannot_answer_impl(self) -> None:
        for screen_id in (None, "", "no-such-screen"):
            query = f"?screen_id={screen_id}" if screen_id is not None else ""
            communicator = WebsocketCommunicator(
                application,
                f"/ws/live/screen/{query}",
                headers=origin_headers(),
            )
            connected, _ = await communicator.connect(
                timeout=CONNECT_TIMEOUT_SECONDS
            )
            self._open_communicators.append(communicator)

            self.assertFalse(connected, f"{screen_id!r} should not be connected")

        self.assertEqual(
            await self._in_thread(stored_answers, self.competition.pk), []
        )

    # -- what other screens see while answering -----------------------------

    def test_other_screens_see_only_a_count_of_answers(self) -> None:
        self._start_question()
        self._run(self._test_other_screens_see_only_a_count_impl)

    async def _test_other_screens_see_only_a_count_impl(self) -> None:
        answering, _ = await self._connect_screen(self.screen_a)
        watcher, _ = await self._connect_screen(self.screen_b)
        await self._skip_intro(answering)
        await self._skip_intro(watcher)

        await send_message(answering, {"type": SUBMIT_ANSWER, "answer": "4"})
        progress, _ = await drain_until(watcher, ANSWER_PROGRESS)

        self.assertEqual(progress["answers_count"], 1)
        self.assertEqual(progress["classes_count"], 2)
        # No classroom is named, so nothing about correctness can be inferred.
        self.assertNotIn("answers", progress)
        self.assertNotIn("classroom_id", progress)

    def test_no_event_before_the_reveal_carries_the_answer_key(self) -> None:
        """The strongest form of the rule.

        Lab A answering correctly must not put the key anywhere in the stream
        Lab B is reading - not in the progress count, not in a tick, and not in
        the question re-sent on a reconnect.
        """
        self._start_question()
        self._run(self._test_no_event_before_the_reveal_carries_the_key_impl)

    async def _test_no_event_before_the_reveal_carries_the_key_impl(self) -> None:
        answering, _ = await self._connect_screen(self.screen_a)
        watcher, _ = await self._connect_screen(self.screen_b)
        await self._skip_intro(answering)
        await self._skip_intro(watcher)

        await send_message(answering, {"type": SUBMIT_ANSWER, "answer": "4"})
        progress, _ = await drain_until(watcher, ANSWER_PROGRESS)

        await send_message(watcher, {"type": "ping"})
        tick, _ = await drain_until(watcher, TICK)

        for message in (progress, tick):
            self.assertNotIn("correct_answer", message)
            self.assertNotIn("correct", message)

        reconnecting, _ = await self._connect_screen(self.screen_b)
        state, _ = await drain_until(reconnecting, STATE_SYNC)
        question = state.get("question") or {}
        self.assertNotIn("correct_answer", question)
        self.assertNotIn("correct_option", question)


class RevealTests(AnswerSubmissionSocketTests):
    """Results become visible when answering closes, and not before.

    Answering closes when the server clock passes the deadline. That is the only
    path to ``answering_closed`` today, so it is the only path to a reveal - and
    that is the point being pinned down: a classroom cannot be shown an outcome
    it has not earned, whatever the clock says.
    """

    async def _close_answering_now(self) -> None:
        """Arm the server clock with a deadline already reached.

        Exactly what happens when a question's real duration elapses, without
        the test waiting for it.
        """
        await clock_runner.start_clock(
            self.competition.pk,
            channel_layer(),
            timezone.now() - timedelta(seconds=1),
        )

    def test_the_reveal_reports_who_was_correct(self) -> None:
        self._start_question()
        self._run(self._test_the_reveal_reports_who_was_correct_impl)

    async def _test_the_reveal_reports_who_was_correct_impl(self) -> None:
        """Lab A right, Lab B wrong: the manual test's scenario, asserted."""
        fast, _ = await self._connect_screen(self.screen_a)
        slow, _ = await self._connect_screen(self.screen_b)
        await self._skip_intro(fast)
        await self._skip_intro(slow)

        await send_message(fast, {"type": SUBMIT_ANSWER, "answer": "4"})
        await drain_until(fast, "answer_accepted")
        await send_message(slow, {"type": SUBMIT_ANSWER, "answer": "5"})
        await drain_until(slow, "answer_accepted")

        await self._close_answering_now()

        reveal, _ = await drain_until(fast, RESULT_REVEALED)

        self.assertEqual(reveal["correct_answer"], "4")
        self.assertEqual(reveal["answers_count"], 2)
        by_class = {row["classroom_id"]: row for row in reveal["answers"]}
        self.assertTrue(by_class[self.lab_a.pk]["is_correct"])
        self.assertFalse(by_class[self.lab_b.pk]["is_correct"])

    def test_the_reveal_reaches_every_screen(self) -> None:
        self._start_question()
        self._run(self._test_the_reveal_reaches_every_screen_impl)

    async def _test_the_reveal_reaches_every_screen_impl(self) -> None:
        fast, _ = await self._connect_screen(self.screen_a)
        slow, _ = await self._connect_screen(self.screen_b)
        await self._skip_intro(fast)
        await self._skip_intro(slow)

        await self._close_answering_now()

        first, _ = await drain_until(fast, RESULT_REVEALED)
        second, _ = await drain_until(slow, RESULT_REVEALED)

        self.assertEqual(first["correct_answer"], second["correct_answer"])

    def test_the_reveal_carries_the_updated_leaderboard(self) -> None:
        self._start_question()
        self._run(self._test_the_reveal_carries_the_updated_leaderboard_impl)

    async def _test_the_reveal_carries_the_updated_leaderboard_impl(self) -> None:
        fast, _ = await self._connect_screen(self.screen_a)
        slow, _ = await self._connect_screen(self.screen_b)
        await self._skip_intro(fast)
        await self._skip_intro(slow)

        await send_message(fast, {"type": SUBMIT_ANSWER, "answer": "4"})
        await drain_until(fast, "answer_accepted")
        await send_message(slow, {"type": SUBMIT_ANSWER, "answer": "5"})
        await drain_until(slow, "answer_accepted")

        await self._close_answering_now()

        reveal, _ = await drain_until(fast, RESULT_REVEALED)

        board = reveal["leaderboard"]
        self.assertEqual(len(board["rows"]), 2)
        by_class = {row["classroom_id"]: row for row in board["rows"]}
        self.assertGreater(by_class[self.lab_a.pk]["score"], 0)
        self.assertEqual(by_class[self.lab_b.pk]["score"], 0)
        self.assertEqual(by_class[self.lab_a.pk]["rank"], 1)

    def test_a_faster_correct_answer_outranks_a_slower_one(self) -> None:
        """The manual test's headline case, asserted rather than eyeballed."""
        self._start_question()
        self._run(self._test_a_faster_correct_answer_outranks_a_slower_one_impl)

    async def _test_a_faster_correct_answer_outranks_a_slower_one_impl(self) -> None:
        fast, _ = await self._connect_screen(self.screen_a)
        slow, _ = await self._connect_screen(self.screen_b)
        await self._skip_intro(fast)
        await self._skip_intro(slow)

        await send_message(fast, {"type": SUBMIT_ANSWER, "answer": "4"})
        await drain_until(fast, "answer_accepted")
        # A real gap, not a mocked one, so the bonus genuinely differs.
        await asyncio.sleep(1.0)
        await send_message(slow, {"type": SUBMIT_ANSWER, "answer": "4"})
        await drain_until(slow, "answer_accepted")

        await self._close_answering_now()

        reveal, _ = await drain_until(fast, RESULT_REVEALED)

        by_class = {row["classroom_id"]: row for row in reveal["leaderboard"]["rows"]}
        self.assertGreater(
            by_class[self.lab_a.pk]["score"], by_class[self.lab_b.pk]["score"]
        )

    def test_the_reveal_does_not_happen_while_answering_is_open(self) -> None:
        """No key, no standings, until the deadline passes."""
        self._start_question()
        self._run(self._test_the_reveal_does_not_happen_while_open_impl)

    async def _test_the_reveal_does_not_happen_while_open_impl(self) -> None:
        watching, _ = await self._connect_screen(self.screen_a)
        answering, _ = await self._connect_screen(self.screen_b)
        await self._skip_intro(watching)
        await self._skip_intro(answering)

        # The other lab answers. This is the one event guaranteed to reach a
        # screen that has not answered itself, so it stands in for everything
        # answering-open traffic: proof that a correct answer is already scored
        # somewhere on the system, and still nothing here reveals it.
        await send_message(answering, {"type": SUBMIT_ANSWER, "answer": "4"})
        await drain_until(answering, "answer_accepted")

        progress, _ = await drain_until(watching, ANSWER_PROGRESS)

        self.assertEqual(progress["answers_count"], 1)
        self.assertNotIn("correct_answer", progress)
        self.assertNotIn("answers", progress)
        self.assertNotIn("leaderboard", progress)

        stored = await self._in_thread(stored_answers, self.competition.pk)
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0].is_correct)

    def test_finishing_the_round_stores_and_publishes_the_final_result(self) -> None:
        self._start_question()
        self._run(
            self._test_finishing_stores_and_publishes_the_final_result_impl
        )

    async def _test_finishing_stores_and_publishes_the_final_result_impl(self) -> None:
        screen, _ = await self._connect_screen(self.screen_a)
        await self._skip_intro(screen)
        await send_message(screen, {"type": SUBMIT_ANSWER, "answer": "4"})
        await drain_until(screen, "answer_accepted")

        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "finish_competition"})

        results, _ = await drain_until(screen, COMPETITION_RESULTS)

        self.assertEqual(results["winner"], "Science Lab A")
        self.assertEqual(len(results["standings"]), 2)

        stored = await self._in_thread(stored_result, self.competition.pk)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.winner_name, "Science Lab A")