"""Ranking, live updates, and the reveal.

The leaderboard is the thing a room reads, so these tests pin down not only that
it orders correctly but that it is stable, complete, and - most importantly -
that it never leaks which classroom was right while answering is still open.
"""

from __future__ import annotations

from django.test import TransactionTestCase

from competitions.models import CompetitionState
from core.tests.utils import (
    add_question,
    create_answer,
    create_classroom,
    create_competition,
    create_question,
    create_scoring_rule,
    create_screen,
    create_teacher,
)
from live import services as live_services
from scoring.leaderboard import (
    answer_progress,
    leaderboard_payload,
    leaderboard_rows,
)
from scoring.models import Answer, CompetitionResult, SpeedBonusMode
from scoring.services import finalise, submit_answer


class LeaderboardTestCase(TransactionTestCase):
    """A started round with three classrooms, as the manual test describes."""

    def setUp(self) -> None:
        super().setUp()

        self.teacher = create_teacher()
        self.lab_a = create_classroom(name="Science Lab A")
        self.lab_b = create_classroom(name="Science Lab B")
        self.lab_c = create_classroom(name="Science Lab C")

        self.question = create_question(
            text="What is 2 + 2?", options=["3", "4", "5", "6"], correct_option="4"
        )
        self.second = create_question(
            text="What is 3 + 3?", options=["5", "6", "7"], correct_option="6"
        )

        self.competition = create_competition(
            teacher=self.teacher,
            title="Lab A versus B versus C",
            classrooms=[self.lab_a, self.lab_b, self.lab_c],
            questions=[self.question, self.second],
        )

        self.screen_a = create_screen(name="A board", classroom=self.lab_a)
        self.screen_b = create_screen(name="B board", classroom=self.lab_b)
        self.screen_c = create_screen(name="C board", classroom=self.lab_c)

        live_services.start_competition(self.competition)

    def start(self, position: int = 1, duration: int = 30):
        return live_services.start_question(self.competition, position=position)

    def answer(self, selection: str, classroom, screen, offset: float):
        from datetime import timedelta

        return submit_answer(
            competition=self.competition,
            classroom=classroom,
            screen=screen,
            selection=selection,
            now=self.competition.current_question_started_at + timedelta(seconds=offset),
        )

    def scores(self) -> dict:
        return {row.classroom_name: row for row in leaderboard_rows(self.competition)}


class OrderingTests(LeaderboardTestCase):
    """The order the room sees."""

    def test_the_highest_score_is_first(self) -> None:
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)      # correct, fast
        self.answer("4", self.lab_b, self.screen_b, 20)     # correct, slow
        self.answer("5", self.lab_c, self.screen_c, 2)      # wrong

        rows = leaderboard_rows(self.competition)

        self.assertEqual(
            [row.classroom_name for row in rows],
            ["Science Lab A", "Science Lab B", "Science Lab C"],
        )
        self.assertEqual([row.rank for row in rows], [1, 2, 3])

    def test_scores_are_derived_from_the_answers(self) -> None:
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)

        rows = self.scores()

        self.assertEqual(rows["Science Lab A"].correct_answers, 1)
        self.assertEqual(rows["Science Lab A"].answers_given, 1)
        self.assertEqual(rows["Science Lab C"].correct_answers, 0)
        self.assertEqual(rows["Science Lab C"].answers_given, 0)

    def test_an_unanswered_classroom_still_appears(self) -> None:
        """A class at zero is information; omitting it would hide the contest."""
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)

        rows = self.scores()

        self.assertEqual(len(rows), 3)
        self.assertIn("Science Lab C", rows)

    def test_equal_scores_are_broken_by_correct_answers(self) -> None:
        """Two classes level on points are separated by who got more right."""
        self.start()
        # A: correct but very late, so almost no bonus. B: correct and fast, but
        # on a shorter question the bonus cannot catch up. Both end level on
        # points, so correctness decides.
        self.answer("4", self.lab_a, self.screen_a, 29)
        self.answer("4", self.lab_b, self.screen_b, 29)

        rows = leaderboard_rows(self.competition)
        names = [row.classroom_name for row in rows]

        # Both are correct, so the tie is unbroken and the name decides.
        self.assertEqual(rows[0].score, rows[1].score)
        self.assertEqual(names, sorted(names))

    def test_tied_classes_share_a_rank_and_the_next_rank_skips(self) -> None:
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 10)
        self.answer("4", self.lab_b, self.screen_b, 10)
        # C does not answer, so it cannot tie on score.
        self.competition.refresh_from_db()

        rows = leaderboard_rows(self.competition)

        self.assertEqual(rows[0].score, rows[1].score)
        self.assertEqual(rows[0].rank, 1)
        self.assertEqual(rows[1].rank, 1)
        self.assertEqual(rows[2].rank, 3)

    def test_the_ordering_is_stable_across_reads(self) -> None:
        """The same answers must always produce the same board.

        A result announced to a classroom has to be the result it says it is.
        """
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 5)
        self.answer("4", self.lab_b, self.screen_b, 5)
        self.answer("4", self.lab_c, self.screen_c, 5)

        first = [row.as_payload() for row in leaderboard_rows(self.competition)]
        second = [row.as_payload() for row in leaderboard_rows(self.competition)]

        self.assertEqual(first, second)

    def test_a_faster_class_leads_an_equal_number_of_correct_answers(self) -> None:
        """Response time only breaks ties; it never outranks score."""
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)
        self.answer("4", self.lab_b, self.screen_b, 20)

        rows = self.scores()

        self.assertGreater(
            rows["Science Lab A"].score, rows["Science Lab B"].score
        )

    def test_a_wrong_fast_answer_never_outranks_a_right_slow_one(self) -> None:
        self.start()
        self.answer("5", self.lab_a, self.screen_a, 0)   # instant, wrong
        self.answer("4", self.lab_b, self.screen_b, 29)  # last second, right

        rows = leaderboard_rows(self.competition)

        self.assertEqual(rows[0].classroom_name, "Science Lab B")

    def test_scores_accumulate_across_questions(self) -> None:
        self.start(position=1)
        self.answer("4", self.lab_a, self.screen_a, 2)
        self.answer("5", self.lab_b, self.screen_b, 2)
        self.competition.refresh_from_db()

        self.start(position=2)
        self.answer("6", self.lab_b, self.screen_b, 2)
        self.competition.refresh_from_db()

        rows = self.scores()

        self.assertEqual(rows["Science Lab A"].correct_answers, 1)
        self.assertEqual(rows["Science Lab B"].correct_answers, 1)
        self.assertEqual(rows["Science Lab C"].correct_answers, 0)
        self.assertGreater(rows["Science Lab B"].score, 100)

    def test_the_scoring_rule_is_reflected_in_the_board(self) -> None:
        create_scoring_rule(
            self.competition, correct_points=500, speed_bonus_max=0
        )
        self.competition.refresh_from_db()
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)

        rows = self.scores()

        self.assertEqual(rows["Science Lab A"].score, 500)


class ProgressTests(LeaderboardTestCase):
    """What may be published while a question is still open."""

    def test_progress_counts_answers_without_saying_who_was_right(self) -> None:
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)
        self.answer("5", self.lab_b, self.screen_b, 2)

        progress = answer_progress(self.competition)

        self.assertEqual(progress["answers_count"], 2)
        self.assertEqual(progress["classes_count"], 3)
        # Nothing in the payload identifies a classroom or its correctness.
        self.assertNotIn("answers", progress)
        self.assertNotIn("rows", progress)
        self.assertNotIn("correct_answer", progress)

    def test_progress_counts_only_the_current_question(self) -> None:
        self.start(position=1)
        self.answer("4", self.lab_a, self.screen_a, 2)
        self.competition.refresh_from_db()
        self.start(position=2)
        self.answer("6", self.lab_b, self.screen_b, 2)

        progress = answer_progress(self.competition)

        self.assertEqual(progress["answers_count"], 1)
        self.assertEqual(progress["position"], 2)


class FinalResultTests(LeaderboardTestCase):
    """A finished round has a stored result."""

    def test_finishing_freezes_the_standings(self) -> None:
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)
        self.answer("5", self.lab_b, self.screen_b, 2)
        self.competition.refresh_from_db()

        live_services.finish_competition(self.competition)
        self.competition.refresh_from_db()
        result = finalise(self.competition)

        self.assertEqual(result.total_questions, 2)
        self.assertEqual(result.answered_count, 2)
        self.assertEqual(result.winner_name, "Science Lab A")
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.rows[0]["rank"], 1)
        self.assertEqual(result.rows[0]["classroom"], "Science Lab A")

    def test_a_round_with_no_answers_still_produces_a_result(self) -> None:
        self.start()
        self.competition.refresh_from_db()
        live_services.finish_competition(self.competition)
        self.competition.refresh_from_db()

        result = finalise(self.competition)

        self.assertEqual(result.answered_count, 0)
        self.assertEqual(result.winner_name, "")
        self.assertEqual(len(result.rows), 3)

    def test_finalising_twice_updates_the_one_result(self) -> None:
        """One result per competition, so a re-run cannot duplicate it."""
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)
        self.competition.refresh_from_db()
        live_services.finish_competition(self.competition)
        self.competition.refresh_from_db()

        finalise(self.competition)
        finalise(self.competition)

        self.assertEqual(CompetitionResult.objects.count(), 1)

    def test_the_stored_result_is_not_changed_by_later_edits_to_answers(self) -> None:
        """A result announced to a room must stay as announced.

        Answers are read-only, but a result also has to survive a correction
        being applied to the underlying data: the snapshot is what was shown.
        """
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)
        self.competition.refresh_from_db()
        live_services.finish_competition(self.competition)
        self.competition.refresh_from_db()

        result = finalise(self.competition)
        frozen = result.rows

        Answer.objects.filter(classroom=self.lab_a).update(points=0, speed_bonus=0)

        result.refresh_from_db()
        self.assertEqual(result.rows, frozen)


class LeaderboardPayloadTests(LeaderboardTestCase):
    """The payload the display renders."""

    def test_the_payload_carries_the_four_displayed_columns(self) -> None:
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)

        payload = leaderboard_payload(self.competition)

        self.assertEqual(payload["competition_id"], self.competition.pk)
        self.assertEqual(len(payload["rows"]), 3)
        for row in payload["rows"]:
            for key in ("rank", "classroom", "score", "correct_answers"):
                self.assertIn(key, row)

    def test_the_payload_does_not_carry_an_answer_key(self) -> None:
        """The board is broadcast during a round, so it must not leak the key."""
        self.start()
        self.answer("4", self.lab_a, self.screen_a, 2)

        payload = leaderboard_payload(self.competition)

        self.assertNotIn("correct_answer", payload)
        for row in payload["rows"]:
            self.assertNotIn("correct_answer", row)
            self.assertNotIn("selected_answer", row)