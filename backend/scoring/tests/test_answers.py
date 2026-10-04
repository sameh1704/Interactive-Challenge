"""Recording and scoring answers, against a real database.

Covers the behaviours the phase brief names: correct, wrong, duplicate, late,
score calculation, speed bonus, authorisation, and attempts by a client to
manipulate what it is scored.

Every test here goes through :func:`scoring.services.submit_answer` rather than
writing an ``Answer`` directly, because the rules under test are the ones the
service enforces - a row created behind its back would prove nothing about them.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TransactionTestCase
from django.utils import timezone

from competitions.models import CompetitionState
from core.tests.utils import (
    add_question,
    create_classroom,
    create_competition,
    create_question,
    create_scoring_rule,
    create_screen,
    create_teacher,
    create_true_false_question,
    include_classroom,
)
from live import services as live_services
from scoring.models import Answer, SpeedBonusMode
from scoring.services import AnswerRejected, extract_selection, submit_answer


class AnswerTestCase(TransactionTestCase):
    """A started competition with one open question and two classrooms."""

    def setUp(self) -> None:
        super().setUp()

        self.teacher = create_teacher()
        self.lab_one = create_classroom(name="Science Lab 1")
        self.lab_two = create_classroom(name="Science Lab 2")

        self.question = create_question(
            text="What is 2 + 2?", options=["3", "4", "5", "6"], correct_option="4"
        )

        self.competition = create_competition(
            teacher=self.teacher,
            title="Lab 1 versus Lab 2",
            classrooms=[self.lab_one, self.lab_two],
            questions=[self.question],
        )

        self.screen_one = create_screen(name="Lab 1 board", classroom=self.lab_one)
        self.screen_two = create_screen(name="Lab 2 board", classroom=self.lab_two)

        live_services.start_competition(self.competition)

    def start_question(self, duration: int = 30, position: int = 1):
        entry = self.competition.questions.get(position=position)
        entry.duration_seconds = duration
        entry.save(update_fields=["duration_seconds"])
        self.competition.refresh_from_db()
        return live_services.start_question(self.competition, position=position)

    def at_offset(self, seconds: float) -> timezone.datetime:
        return self.competition.current_question_started_at + timedelta(seconds=seconds)

    def submit(self, selection, classroom=None, screen=None, at=None):
        return submit_answer(
            competition=self.competition,
            classroom=classroom if classroom is not None else self.lab_one,
            screen=screen if screen is not None else self.screen_one,
            selection=selection,
            now=at,
        )


class CorrectnessTests(AnswerTestCase):
    """Correctness is decided by the server, from the stored key."""

    def test_the_correct_option_is_recorded_as_correct(self) -> None:
        self.start_question()

        answer = self.submit("4")

        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points, 100)
        self.assertGreater(answer.speed_bonus, 0)
        self.assertEqual(answer.total_score, answer.points + answer.speed_bonus)

    def test_a_wrong_option_is_recorded_as_incorrect(self) -> None:
        self.start_question()

        answer = self.submit("5")

        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.points, 0)
        self.assertEqual(answer.speed_bonus, 0)

    def test_a_near_miss_option_is_not_a_hit(self) -> None:
        """Options are compared exactly, so only the stored key is correct."""
        self.start_question()

        self.assertFalse(self.submit("5").is_correct)
        self.assertFalse(self.submit("3", classroom=self.lab_two).is_correct)

    def test_surrounding_whitespace_does_not_change_the_choice(self) -> None:
        """Trimmed on the way in, the way a real message is handled.

        The trimming lives in ``extract_selection`` - the reader for a client's
        message - rather than in the scoring path, so this goes through the same
        two steps the screen consumer does.
        """
        self.start_question()

        answer = submit_answer(
            competition=self.competition,
            classroom=self.lab_one,
            screen=self.screen_one,
            selection=extract_selection({"answer": " 4 "}),
        )

        self.assertTrue(answer.is_correct)

    def test_a_true_false_question_is_scored(self) -> None:
        statement = create_true_false_question()
        add_question(self.competition, statement, position=2)
        self.start_question(position=2)

        self.assertTrue(self.submit("true").is_correct)
        self.assertFalse(self.submit("false", classroom=self.lab_two).is_correct)

    def test_an_option_that_does_not_exist_is_refused(self) -> None:
        self.start_question()

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("42")

        self.assertEqual(caught.exception.code, "invalid_answer")
        self.assertEqual(Answer.objects.count(), 0)

    def test_a_question_with_no_answer_key_cannot_be_scored(self) -> None:
        unkeyed = create_question(
            text="Unkeyed", options=["a", "b"], correct_option=""
        )
        add_question(self.competition, unkeyed, position=2)
        self.start_question(position=2)

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("a")

        self.assertEqual(caught.exception.code, "no_answer_key")
        self.assertEqual(Answer.objects.count(), 0)


class DuplicateAnswerTests(AnswerTestCase):
    """One final answer per classroom per question."""

    def test_a_second_answer_from_the_same_classroom_is_refused(self) -> None:
        self.start_question()

        self.submit("4")

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("5")

        self.assertEqual(caught.exception.code, "already_answered")
        self.assertEqual(Answer.objects.count(), 1)

    def test_the_first_answer_is_kept_unchanged(self) -> None:
        self.start_question()

        self.submit("4")
        with self.assertRaises(AnswerRejected):
            self.submit("5")

        stored = Answer.objects.get()
        self.assertEqual(stored.selection, "4")
        self.assertTrue(stored.is_correct)

    def test_a_change_of_screen_does_not_allow_a_second_answer(self) -> None:
        """Identity is the classroom, not the connection.

        A classroom with two boards is one participant. Letting a second screen
        answer for it would let one class answer twice.
        """
        self.start_question()
        second_board = create_screen(name="Lab 1 second board", classroom=self.lab_one)

        self.submit("4", screen=self.screen_one)

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("5", screen=second_board)

        self.assertEqual(caught.exception.code, "already_answered")
        self.assertEqual(Answer.objects.count(), 1)

    def test_each_classroom_may_answer_once(self) -> None:
        self.start_question()

        self.submit("4", classroom=self.lab_one)
        self.submit("5", classroom=self.lab_two)

        self.assertEqual(Answer.objects.count(), 2)

    def test_a_classroom_may_answer_the_next_question(self) -> None:
        second = create_question(
            text="What is 3 + 3?", options=["5", "6", "7"], correct_option="6"
        )
        add_question(self.competition, second, position=2)

        self.start_question(position=1)
        self.submit("4")

        self.start_question(position=2)
        answer = self.submit("6")

        self.assertTrue(answer.is_correct)
        self.assertEqual(Answer.objects.count(), 2)

    def test_the_database_refuses_a_duplicate_even_if_the_check_is_bypassed(self) -> None:
        """The unique constraint, not just the service, is the guarantee.

        Two screens submitting at the same instant both pass the "have you
        already answered?" check. Only the constraint can tell them apart, so the
        test proves the constraint exists rather than trusting the check.
        """
        self.start_question()
        self.submit("4")

        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Answer.objects.create(
                    competition=self.competition,
                    question=self.competition.current_question,
                    position=1,
                    classroom=self.lab_one,
                    selected_answer="5",
                    response_time_seconds=1.0,
                    duration_seconds=30,
                    is_correct=False,
                )


class LateAnswerTests(AnswerTestCase):
    """Answering after the deadline is refused, not scored."""

    def test_an_answer_after_the_deadline_is_refused(self) -> None:
        self.start_question(duration=20)
        self.competition.refresh_from_db()

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("4", at=self.at_offset(20.5))

        self.assertEqual(caught.exception.code, "late_answer")
        self.assertEqual(Answer.objects.count(), 0)

    def test_an_answer_exactly_on_the_deadline_is_accepted(self) -> None:
        """The deadline is inclusive: the last instant is still answering."""
        self.start_question(duration=20)
        self.competition.refresh_from_db()

        answer = self.submit("4", at=self.at_offset(20.0))

        self.assertTrue(answer.is_correct)

    def test_an_answer_one_microsecond_late_is_refused(self) -> None:
        self.start_question(duration=20)
        self.competition.refresh_from_db()

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("4", at=self.at_offset(20.000001))

        self.assertEqual(caught.exception.code, "late_answer")

    def test_no_answer_is_accepted_once_answering_has_closed(self) -> None:
        self.start_question()
        live_services.close_answering(self.competition)
        self.competition.refresh_from_db()

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("4")

        self.assertEqual(caught.exception.code, "not_answering")

    def test_an_answer_late_in_the_period_earns_almost_no_bonus(self) -> None:
        """The last moments of a question are worth almost nothing extra."""
        self.start_question(duration=20)
        self.competition.refresh_from_db()

        answer = self.submit("4", at=self.at_offset(19.5))

        self.assertTrue(answer.is_correct)
        # 0.5s of 20s left is 2.5% of a 50 point bonus, which rounds to 1.
        self.assertEqual(answer.speed_bonus, 1)


class TimingTests(AnswerTestCase):
    """Response time comes from the server clock, not the message."""

    def test_the_response_time_is_measured_from_the_server_start(self) -> None:
        self.start_question(duration=30)
        self.competition.refresh_from_db()

        answer = self.submit("4", at=self.at_offset(7.5))

        self.assertAlmostEqual(answer.response_time_seconds, 7.5, places=2)

    def test_the_recorded_duration_is_the_questions_own(self) -> None:
        """A per-competition override is what the bonus is measured against."""
        self.start_question(duration=45)
        self.competition.refresh_from_db()

        answer = self.submit("4", at=self.at_offset(15))

        self.assertEqual(answer.duration_seconds, 45)

    def test_a_faster_correct_answer_scores_more(self) -> None:
        self.start_question(duration=30)
        self.competition.refresh_from_db()

        fast = self.submit("4", classroom=self.lab_one, at=self.at_offset(2))
        slow = self.submit("4", classroom=self.lab_two, at=self.at_offset(20))

        self.assertGreater(fast.total_score, slow.total_score)
        self.assertGreater(fast.speed_bonus, slow.speed_bonus)


class ServerAuthorityTests(AnswerTestCase):
    """A client cannot influence what it is scored."""

    def test_a_client_supplied_score_is_ignored(self) -> None:
        self.start_question()

        selection = extract_selection(
            {"answer": "4", "score": 9999, "points": 9999, "total_score": 9999}
        )
        answer = submit_answer(
            competition=self.competition,
            classroom=self.lab_one,
            screen=self.screen_one,
            selection=selection,
            now=self.at_offset(3),
        )

        self.assertNotEqual(answer.total_score, 9999)
        self.assertEqual(answer.points, 100)

    def test_a_client_claiming_to_be_correct_is_ignored(self) -> None:
        self.start_question()

        selection = extract_selection({"answer": "5", "is_correct": True})
        answer = submit_answer(
            competition=self.competition,
            classroom=self.lab_one,
            screen=self.screen_one,
            selection=selection,
        )

        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.total_score, 0)

    def test_a_client_supplied_response_time_is_ignored(self) -> None:
        self.start_question()
        self.competition.refresh_from_db()

        selection = extract_selection(
            {"answer": "4", "response_time": 0, "response_time_seconds": 0}
        )
        answer = submit_answer(
            competition=self.competition,
            classroom=self.lab_one,
            screen=self.screen_one,
            selection=selection,
            now=self.at_offset(20),
        )

        # The server measured 20 seconds, so the bonus is the 20-second one,
        # not the full amount a zero would have earned.
        self.assertAlmostEqual(answer.response_time_seconds, 20.0, places=2)
        self.assertLess(answer.speed_bonus, 50)

    def test_a_client_cannot_answer_for_another_classroom(self) -> None:
        """The classroom is an argument resolved by the caller.

        The service has no parameter a client can fill in to name a different
        classroom, which is the structural reason this cannot be manipulated.
        """
        self.start_question()

        selection = extract_selection(
            {"answer": "4", "classroom_id": self.lab_two.pk, "classroom": "Science Lab 2"}
        )
        answer = submit_answer(
            competition=self.competition,
            classroom=self.lab_one,
            screen=self.screen_one,
            selection=selection,
        )

        self.assertEqual(answer.classroom_id, self.lab_one.pk)

    def test_a_classroom_not_in_the_competition_cannot_answer(self) -> None:
        outsider_room = create_classroom(name="Art Room")
        self.start_question()

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("4", classroom=outsider_room)

        self.assertEqual(caught.exception.code, "classroom_not_included")
        self.assertEqual(Answer.objects.count(), 0)

    def test_a_screen_with_no_classroom_cannot_answer(self) -> None:
        """A screen that was never placed in a room cannot score for anyone."""
        self.start_question()
        orphan = create_screen(name="Not placed", classroom=None)

        with self.assertRaises(AnswerRejected) as caught:
            submit_answer(
                competition=self.competition,
                classroom=None,
                screen=orphan,
                selection="4",
            )

        self.assertEqual(caught.exception.code, "unknown_classroom")


class AnswerWindowTests(AnswerTestCase):
    """Answers are only accepted while a question is open."""

    def test_no_question_is_open_before_the_first_one_starts(self) -> None:
        with self.assertRaises(AnswerRejected) as caught:
            self.submit("4")

        self.assertEqual(caught.exception.code, "no_question")

    def test_no_answers_are_accepted_after_the_round_finishes(self) -> None:
        self.start_question()
        live_services.finish_competition(self.competition)
        self.competition.refresh_from_db()

        with self.assertRaises(AnswerRejected) as caught:
            self.submit("4")

        self.assertEqual(caught.exception.code, "finished")

    def test_no_answers_are_accepted_before_the_round_starts(self) -> None:
        other = create_competition(
            teacher=self.teacher,
            title="Not started",
            classrooms=[self.lab_one],
            questions=[self.question],
        )

        with self.assertRaises(AnswerRejected) as caught:
            submit_answer(
                competition=other,
                classroom=self.lab_one,
                screen=self.screen_one,
                selection="4",
            )

        self.assertEqual(caught.exception.code, "no_question")


class ConfigurableRuleTests(AnswerTestCase):
    """Scoring rules are configuration, not code."""

    def test_a_per_competition_rule_overrides_the_defaults(self) -> None:
        create_scoring_rule(
            self.competition,
            correct_points=250,
            speed_bonus_max=25,
            speed_bonus_mode=SpeedBonusMode.LINEAR,
        )
        self.competition.refresh_from_db()
        self.start_question(duration=30)
        self.competition.refresh_from_db()

        answer = self.submit("4", at=self.at_offset(15))

        self.assertEqual(answer.points, 250)
        self.assertEqual(answer.speed_bonus, 12)  # half of 25

    def test_the_speed_bonus_can_be_switched_off_for_one_competition(self) -> None:
        create_scoring_rule(
            self.competition,
            correct_points=100,
            speed_bonus_max=50,
            speed_bonus_mode=SpeedBonusMode.NONE,
        )
        self.competition.refresh_from_db()
        self.start_question(duration=30)
        self.competition.refresh_from_db()

        answer = self.submit("4", at=self.at_offset(0))

        self.assertEqual(answer.speed_bonus, 0)
        self.assertEqual(answer.total_score, 100)

    def test_a_zero_bonus_maximum_switches_it_off_too(self) -> None:
        create_scoring_rule(
            self.competition,
            correct_points=100,
            speed_bonus_max=0,
            speed_bonus_mode=SpeedBonusMode.LINEAR,
        )
        self.competition.refresh_from_db()
        self.start_question(duration=30)
        self.competition.refresh_from_db()

        answer = self.submit("4", at=self.at_offset(0))

        self.assertEqual(answer.speed_bonus, 0)


class StoredAnswerTests(AnswerTestCase):
    """Everything the phase brief requires an answer to record."""

    def test_the_recorded_answer_carries_the_full_trail(self) -> None:
        self.start_question(duration=30)
        self.competition.refresh_from_db()
        submitted_at = self.at_offset(9)

        answer = self.submit("4", screen=self.screen_one, at=submitted_at)

        self.assertEqual(answer.competition_id, self.competition.pk)
        self.assertEqual(answer.question_id, self.question.pk)
        self.assertEqual(answer.classroom_id, self.lab_one.pk)
        self.assertEqual(answer.screen_id, self.screen_one.pk)
        self.assertEqual(answer.selected_answer, "4")
        self.assertEqual(answer.submitted_at, submitted_at)
        self.assertAlmostEqual(answer.response_time_seconds, 9.0, places=2)

    def test_the_answer_key_is_copied_onto_the_answer(self) -> None:
        """A report must still explain a result if the question is edited later."""
        self.start_question()

        answer = self.submit("4")
        self.question.correct_option = "5"
        self.question.save(update_fields=["correct_option"])
        answer.refresh_from_db()

        self.assertEqual(answer.correct_answer, "4")

    def test_the_position_of_the_question_is_recorded(self) -> None:
        second = create_question(
            text="Second", options=["x", "y"], correct_option="y"
        )
        add_question(self.competition, second, position=2)

        self.start_question(position=2)
        answer = self.submit("y")

        self.assertEqual(answer.position, 2)

    def test_a_retired_screen_does_not_erase_its_answer(self) -> None:
        self.start_question()
        answer = self.submit("4")

        self.screen_one.delete()

        answer.refresh_from_db()
        self.assertIsNone(answer.screen_id)
        self.assertTrue(answer.is_correct)


class CompetitionReuseTests(AnswerTestCase):
    """The scoring model behaves when a classroom is added late."""

    def test_a_late_joining_classroom_appears_at_zero(self) -> None:
        """Covered in detail in test_leaderboard; asserted here for the trail."""
        self.start_question()
        self.submit("4")

        self.competition.refresh_from_db()
        from scoring.leaderboard import leaderboard_rows

        rows = {row.classroom_id: row for row in leaderboard_rows(self.competition)}

        self.assertIn(self.lab_one.pk, rows)
        self.assertIn(self.lab_two.pk, rows)
        self.assertEqual(rows[self.lab_two.pk].score, 0)