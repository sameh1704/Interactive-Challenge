"""The new question types, answered and scored over a real WebSocket.

The unit tests in ``questions.tests`` cover each type's rules on their own. These
cover the whole path a classroom actually takes: every screen connects, the
teacher starts the round and the question, each screen receives the same
``question_started``, a classroom submits, is told only that its answer was
recorded, and the server decides the score.

Every screen is connected *before* the question starts, which is the only honest
ordering. A screen that joins after the fact is sent the current question in
``state_sync`` rather than as a ``question_started`` event, and a reveal that has
already happened is not re-sent to it at all - so a test that connected late would
be testing reconnection rather than the question type.

The guarantee worth repeating is the last one: for an ordering or classification
question the answer key is a whole structure, which is far easier to leak by
accident than a single string. So each type asserts that the open question
mentions nothing of its key, and that the key appears only at the reveal.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async

from core.tests.utils import (
    create_classification_question,
    create_classroom,
    create_competition,
    create_ordering_question,
    create_screen,
    create_short_answer_question,
    create_teacher,
)
from live import services
from live.events import QUESTION_STARTED, RESULT_REVEALED, SUBMIT_ANSWER
from live.tests.support import drain_until, send_message
from live.tests.test_live_engine import LiveTestCase
from scoring.models import Answer

EXPLANATION = "Because that is how it works."


def build(func, *args, **kwargs):
    return async_to_sync(database_sync_to_async(func))(*args, **kwargs)


class QuestionTypeScoringTests(LiveTestCase):
    """Two labs, one question of the subclass's type, scored over a socket."""

    question_factory = None
    question_kwargs: dict = {}

    def setUp(self) -> None:
        super().setUp()

        self.teacher = build(create_teacher)
        self.lab_a = build(create_classroom, name="Science Lab A")
        self.lab_b = build(create_classroom, name="Science Lab B")
        self.question = build(self.question_factory, **self.question_kwargs)
        # Set on the question rather than per subclass, so every type is revealed
        # with an explanation and the "must not leak yet" check is uniform.
        self.question.explanation = EXPLANATION
        build(self.question.save, update_fields=["explanation"])
        self.competition = build(
            create_competition,
            teacher=self.teacher,
            title="A versus B",
            classrooms=[self.lab_a, self.lab_b],
            questions=[self.question],
        )
        self.screen_a = build(create_screen, name="A board", classroom=self.lab_a)
        self.screen_b = build(create_screen, name="B board", classroom=self.lab_b)

        # The round starts, but the question does not: each test opens the scene
        # itself so that its screens are attached before the question goes out.
        build(services.start_competition, self.competition)

        self.teacher_socket = None
        self.sockets: list = []
        self.question_events: list = []

    # -- the scene ---------------------------------------------------------

    async def _open_scene(self, screens=("a", "b")) -> None:
        """Connect the teacher and the named screens, then start the question.

        Returns once every requested screen has received the same
        ``question_started``, which is also the assertion that they all did.
        """
        self.teacher_socket, _ = await self._connect_teacher(
            self.teacher, self.competition
        )
        await self._skip_intro(self.teacher_socket)

        wanted = [self.screen_a, self.screen_b] if "a" in screens else []
        if "b" in screens:
            wanted.append(self.screen_b)

        self.sockets = []
        for screen in wanted:
            communicator, _ = await self._connect_screen(screen)
            await self._skip_intro(communicator)
            self.sockets.append(communicator)

        await send_message(self.teacher_socket, {"type": "start_question"})

        # The question events are kept rather than discarded. A test that needs to
        # assert on them cannot wait for a second one - the event has already
        # been consumed by making sure it arrived.
        self.question_events = []
        for communicator in self.sockets:
            message, _ = await drain_until(communicator, QUESTION_STARTED)
            self.question_events.append(message)

    def _socket(self, label: str):
        return self.sockets[0] if label == "a" else self.sockets[1]

    def _run(self, impl):
        super()._run(impl)

    async def _answer(self, label: str, answer) -> list:
        """Submit one answer and assert the acknowledgement reveals nothing.

        Returns every message consumed on the way to the acknowledgement, so a
        test can inspect what the room was sent before it got one.
        """
        communicator = self._socket(label)
        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": answer})
        accepted, seen = await drain_until(communicator, "answer_accepted")

        self.assertNotIn("is_correct", accepted)
        self.assertNotIn("correct_answer", accepted)
        self.assertNotIn("score", accepted)
        return seen

    async def _refused(self, label: str, answer, code: str) -> None:
        communicator = self._socket(label)
        await send_message(communicator, {"type": SUBMIT_ANSWER, "answer": answer})
        error, _ = await drain_until(communicator, "error")

        self.assertEqual(error["code"], code)

    async def _end_and_reveal(self, label: str = "a") -> dict:
        """End the question through the teacher and read the reveal."""
        await send_message(self.teacher_socket, {"type": "end_question"})
        reveal, _ = await drain_until(self._socket(label), RESULT_REVEALED)
        return reveal

    async def _stored(self):
        return await self._in_thread(
            lambda: list(Answer.objects.for_competition(self.competition.pk))
        )


class OrderingScoringTests(QuestionTypeScoringTests):
    question_factory = staticmethod(create_ordering_question)
    question_kwargs = {
        "text": "Put these in the order they grow.",
        "items": ["seed", "sprout", "tree"],
        "correct_order": ["seed", "sprout", "tree"],
    }

    def test_both_screens_receive_the_same_question(self) -> None:
        self._run(self._test_both_screens_receive_the_same_question_impl)

    async def _test_both_screens_receive_the_same_question_impl(self) -> None:
        await self._open_scene()
        first, second = self.question_events[0], self.question_events[1]

        self.assertEqual(first["text"], second["text"])
        self.assertEqual(
            first["presentation"]["items"], second["presentation"]["items"]
        )

    def test_the_open_question_carries_no_key(self) -> None:
        self._run(self._test_the_open_question_carries_no_key_impl)

    async def _test_the_open_question_carries_no_key_impl(self) -> None:
        await self._open_scene()
        body = str(self.question_events[0])

        for leaked in (
            "correct_order",
            "assignments",
            "accepted_answers",
            "correct_answer",
            "explanation",
        ):
            self.assertNotIn(leaked, body)

    def test_a_correct_order_scores(self) -> None:
        self._run(self._test_a_correct_order_scores_impl)

    async def _test_a_correct_order_scores_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", ["seed", "sprout", "tree"])

        answer = (await self._stored())[0]
        self.assertTrue(answer.is_correct)
        self.assertGreater(answer.total_score, 0)

    def test_a_wrong_order_scores_nothing(self) -> None:
        self._run(self._test_a_wrong_order_scores_nothing_impl)

    async def _test_a_wrong_order_scores_nothing_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", ["sprout", "seed", "tree"])

        answer = (await self._stored())[0]
        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.total_score, 0)

    def test_an_incomplete_order_is_refused(self) -> None:
        self._run(self._test_an_incomplete_order_is_refused_impl)

    async def _test_an_incomplete_order_is_refused_impl(self) -> None:
        await self._open_scene()
        await self._refused("b", ["seed", "sprout"], "invalid_answer")

    def test_a_repeated_item_is_refused(self) -> None:
        self._run(self._test_a_repeated_item_is_refused_impl)

    async def _test_a_repeated_item_is_refused_impl(self) -> None:
        await self._open_scene()
        await self._refused("b", ["seed", "seed", "tree"], "invalid_answer")

    def test_the_stored_answer_keeps_the_sequence(self) -> None:
        self._run(self._test_the_stored_answer_keeps_the_sequence_impl)

    async def _test_the_stored_answer_keeps_the_sequence_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", ["seed", "sprout", "tree"])

        answer = (await self._stored())[0]
        self.assertEqual(answer.selected_answer, ["seed", "sprout", "tree"])
        self.assertEqual(answer.selection, "seed → sprout → tree")

    def test_the_reveal_shows_the_correct_order(self) -> None:
        self._run(self._test_the_reveal_shows_the_correct_order_impl)

    async def _test_the_reveal_shows_the_correct_order_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", ["seed", "sprout", "tree"])

        reveal = await self._end_and_reveal()

        self.assertEqual(reveal["correct_answer"], "seed > sprout > tree")

    def test_a_second_answer_from_the_same_classroom_is_refused(self) -> None:
        self._run(self._test_a_second_answer_is_refused_impl)

    async def _test_a_second_answer_is_refused_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", ["seed", "sprout", "tree"])

        await self._refused("b", ["tree", "sprout", "seed"], "already_answered")


class ClassificationScoringTests(QuestionTypeScoringTests):
    question_factory = staticmethod(create_classification_question)
    question_kwargs = {
        "text": "Place each item.",
        "categories": ["Solid", "Liquid"],
        "assignments": [("ice", "Solid"), ("water", "Liquid")],
    }

    def test_the_open_question_carries_no_key(self) -> None:
        self._run(self._test_the_open_question_carries_no_key_impl)

    async def _test_the_open_question_carries_no_key_impl(self) -> None:
        await self._open_scene()
        body = str(self.question_events[0])

        for leaked in ("assignments", "correct_answer", "explanation"):
            self.assertNotIn(leaked, body)

        # The screen is given the categories and the items to place - which it
        # must have to render the question - and nothing about where they go.
        self.assertEqual(self.question_events[0]["presentation"]["categories"], ["Solid", "Liquid"])
        self.assertEqual(self.question_events[0]["presentation"]["items"], ["ice", "water"])

    def test_correct_placements_score(self) -> None:
        self._run(self._test_correct_placements_score_impl)

    async def _test_correct_placements_score_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", {"ice": "Solid", "water": "Liquid"})

        answer = (await self._stored())[0]
        self.assertTrue(answer.is_correct)

    def test_a_list_of_placements_scores_too(self) -> None:
        self._run(self._test_a_list_of_placements_scores_too_impl)

    async def _test_a_list_of_placements_scores_too_impl(self) -> None:
        await self._open_scene()
        await self._answer(
            "b",
            [
                {"label": "ice", "category": "Solid"},
                {"label": "water", "category": "Liquid"},
            ],
        )

        answer = (await self._stored())[0]
        self.assertTrue(answer.is_correct)

    def test_one_wrong_placement_scores_nothing(self) -> None:
        self._run(self._test_one_wrong_placement_scores_nothing_impl)

    async def _test_one_wrong_placement_scores_nothing_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", {"ice": "Liquid", "water": "Liquid"})

        answer = (await self._stored())[0]
        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.total_score, 0)

    def test_a_placement_in_an_unknown_category_is_refused(self) -> None:
        self._run(self._test_an_unknown_category_is_refused_impl)

    async def _test_an_unknown_category_is_refused_impl(self) -> None:
        await self._open_scene()
        await self._refused(
            "b", {"ice": "Solid", "water": "Gas"}, "invalid_answer"
        )

    def test_a_missing_item_is_refused(self) -> None:
        self._run(self._test_a_missing_item_is_refused_impl)

    async def _test_a_missing_item_is_refused_impl(self) -> None:
        await self._open_scene()
        await self._refused("b", {"ice": "Solid"}, "invalid_answer")

    def test_the_stored_answer_keeps_the_mapping(self) -> None:
        self._run(self._test_the_stored_answer_keeps_the_mapping_impl)

    async def _test_the_stored_answer_keeps_the_mapping_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", {"ice": "Solid", "water": "Liquid"})

        answer = (await self._stored())[0]
        self.assertEqual(
            answer.selected_answer, {"ice": "Solid", "water": "Liquid"}
        )

    def test_the_reveal_shows_every_placement(self) -> None:
        self._run(self._test_the_reveal_shows_every_placement_impl)

    async def _test_the_reveal_shows_every_placement_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", {"ice": "Solid", "water": "Liquid"})

        reveal = await self._end_and_reveal()

        self.assertEqual(
            reveal["correct_answer"], "ice = Solid, water = Liquid"
        )


class ShortAnswerScoringTests(QuestionTypeScoringTests):
    question_factory = staticmethod(create_short_answer_question)
    question_kwargs = {
        "text": "What process do plants use to make food?",
        "accepted_answers": ["photosynthesis"],
    }

    def test_the_open_question_carries_no_key(self) -> None:
        self._run(self._test_the_open_question_carries_no_key_impl)

    async def _test_the_open_question_carries_no_key_impl(self) -> None:
        await self._open_scene()
        body = str(self.question_events[0])

        for leaked in ("accepted_answers", "correct_answer", "explanation"):
            self.assertNotIn(leaked, body)

    def test_the_exact_answer_scores(self) -> None:
        self._run(self._test_the_exact_answer_scores_impl)

    async def _test_the_exact_answer_scores_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", "photosynthesis")

        answer = (await self._stored())[0]
        self.assertTrue(answer.is_correct)

    def test_surrounding_and_repeated_spaces_do_not_matter(self) -> None:
        self._run(self._test_spaces_do_not_matter_impl)

    async def _test_spaces_do_not_matter_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", "  photosynthesis  ")

        answer = (await self._stored())[0]
        self.assertTrue(answer.is_correct)

    def test_case_does_not_matter_by_default(self) -> None:
        self._run(self._test_case_does_not_matter_impl)

    async def _test_case_does_not_matter_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", "PhotoSynthesis")

        answer = (await self._stored())[0]
        self.assertTrue(answer.is_correct)

    def test_a_different_word_scores_nothing(self) -> None:
        self._run(self._test_a_different_word_scores_nothing_impl)

    async def _test_a_different_word_scores_nothing_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", "respiration")

        answer = (await self._stored())[0]
        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.total_score, 0)

    def test_a_near_miss_is_not_accepted(self) -> None:
        self._run(self._test_a_near_miss_is_not_accepted_impl)

    async def _test_a_near_miss_is_not_accepted_impl(self) -> None:
        # No fuzzy matching: this project will not mark a pupil right for a word
        # they have not got.
        await self._open_scene()
        await self._answer("b", "photosynthesising")

        answer = (await self._stored())[0]
        self.assertFalse(answer.is_correct)

    def test_a_blank_answer_is_refused(self) -> None:
        self._run(self._test_a_blank_answer_is_refused_impl)

    async def _test_a_blank_answer_is_refused_impl(self) -> None:
        await self._open_scene()
        await self._refused("b", "   ", "missing_answer")

    def test_the_reveal_shows_the_expected_answer(self) -> None:
        self._run(self._test_the_reveal_shows_the_expected_answer_impl)

    async def _test_the_reveal_shows_the_expected_answer_impl(self) -> None:
        await self._open_scene()
        await self._answer("b", "photosynthesis")

        reveal = await self._end_and_reveal()

        self.assertEqual(reveal["correct_answer"], "photosynthesis")
        self.assertEqual(reveal["explanation"], EXPLANATION)

    def test_an_explanation_never_appears_before_the_reveal(self) -> None:
        self._run(self._test_explanation_never_appears_before_reveal_impl)

    async def _test_explanation_never_appears_before_reveal_impl(self) -> None:
        await self._open_scene()

        # Everything this screen was sent before its own answer was acknowledged.
        # `drain_until` returns every message it consumed on the way to the
        # match, so this is the real history rather than a sample.
        seen = await self._answer("b", "photosynthesis")

        for message in seen:
            self.assertNotIn(EXPLANATION, str(message))
            self.assertNotEqual(message.get("type"), RESULT_REVEALED)

        # And the question event itself, which is where a leak would most likely
        # be, since it carries the most data.
        self.assertNotIn(EXPLANATION, str(self.question_events[0]))


class ClientAuthorityTests(QuestionTypeScoringTests):
    """A client can state its answer and nothing else."""

    question_factory = staticmethod(create_short_answer_question)
    question_kwargs = {
        "text": "What process do plants use to make food?",
        "accepted_answers": ["photosynthesis"],
    }

    def test_a_client_cannot_claim_a_score_or_an_identity(self) -> None:
        self._run(self._test_a_client_cannot_claim_a_score_impl)

    async def _test_a_client_cannot_claim_a_score_impl(self) -> None:
        await self._open_scene()

        await send_message(
            self._socket("b"),
            {
                "type": SUBMIT_ANSWER,
                "answer": "respiration",
                "is_correct": True,
                "correct_answer": "respiration",
                "score": 100000,
                "points": 100000,
                "speed_bonus": 100000,
                "total_score": 100000,
                "classroom_id": self.lab_a.pk,
                "screen_id": self.screen_a.pk,
                "response_time": 0.01,
                "submitted_at": "2099-01-01T00:00:00Z",
            },
        )
        await drain_until(self._socket("b"), "answer_accepted")

        answers = await self._stored()
        self.assertEqual(len(answers), 1)
        answer = answers[0]

        # A wrong answer, so nothing scored...
        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.total_score, 0)
        # ...recorded against the screen's own classroom, not the one it named...
        self.assertEqual(answer.classroom_id, self.lab_b.pk)
        self.assertEqual(answer.screen_id, self.screen_b.pk)
        # ...and scored against the stored key, not the one it supplied.
        self.assertEqual(answer.correct_answer, "photosynthesis")
        # The server's own clock, not the timestamp it sent.
        self.assertLess(answer.submitted_at.year, 2099)

    def test_a_screen_cannot_end_the_question(self) -> None:
        self._run(self._test_a_screen_cannot_end_the_question_impl)

    async def _test_a_screen_cannot_end_the_question_impl(self) -> None:
        await self._open_scene()

        await send_message(self._socket("b"), {"type": "end_question"})
        error, _ = await drain_until(self._socket("b"), "error")

        self.assertEqual(error["code"], "unknown_message")

        # The question is still open, so it is still answerable.
        await self._answer("a", "photosynthesis")
        self.assertTrue((await self._stored())[0].is_correct)
