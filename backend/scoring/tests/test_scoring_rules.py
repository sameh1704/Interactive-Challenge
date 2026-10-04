"""Scoring rules as pure arithmetic.

These tests exercise :mod:`scoring.services`'s scoring functions with no
database, no sockets and no competition. That is the point of keeping them
separate: the numbers that decide a school's results are checked directly, so a
failure here can only ever mean the arithmetic is wrong.

The database-backed behaviour - who may answer, what happens twice, what happens
late - is in ``test_answers.py``.
"""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from scoring.models import SpeedBonusMode
from scoring.services import (
    AnswerRejected,
    extract_selection,
    score_answer,
    speed_bonus_for,
)

FIFTY_SECONDS = 50


def rule(**overrides) -> "ScoringRuleValues":
    from scoring.models import ScoringRuleValues

    defaults = {
        "correct_points": 100,
        "speed_bonus_max": FIFTY_SECONDS,
        "speed_bonus_mode": SpeedBonusMode.LINEAR,
    }
    defaults.update(overrides)
    return ScoringRuleValues(**defaults)


class SpeedBonusTests(SimpleTestCase):
    """The bonus decays linearly from the full amount to nothing."""

    def test_answering_immediately_earns_the_full_bonus(self) -> None:
        self.assertEqual(speed_bonus_for(0.0, 30, rule()), FIFTY_SECONDS)

    def test_the_bonus_halves_at_the_halfway_point(self) -> None:
        self.assertEqual(speed_bonus_for(15.0, 30, rule()), 25)

    def test_the_bonus_is_nothing_at_the_deadline(self) -> None:
        self.assertEqual(speed_bonus_for(30.0, 30, rule()), 0)

    def test_answering_after_the_deadline_earns_nothing(self) -> None:
        self.assertEqual(speed_bonus_for(45.0, 30, rule()), 0)

    def test_the_bonus_never_exceeds_the_configured_maximum(self) -> None:
        # A negative elapsed time is impossible through the service, which
        # clamps at zero; the function is defended anyway because it is the
        # thing that decides a result.
        self.assertEqual(speed_bonus_for(-10.0, 30, rule()), FIFTY_SECONDS)

    def test_the_bonus_never_exceeds_the_configured_maximum_when_reconfigured(self) -> None:
        smaller = rule(speed_bonus_max=20)
        self.assertEqual(speed_bonus_for(0.0, 30, smaller), 20)
        self.assertEqual(speed_bonus_for(15.0, 30, smaller), 10)

    def test_a_zero_bonus_maximum_disables_the_bonus(self) -> None:
        self.assertEqual(speed_bonus_for(0.0, 30, rule(speed_bonus_max=0)), 0)

    def test_the_none_mode_disables_the_bonus(self) -> None:
        self.assertEqual(
            speed_bonus_for(0.0, 30, rule(speed_bonus_mode=SpeedBonusMode.NONE)), 0
        )

    def test_a_zero_length_question_pays_the_full_bonus(self) -> None:
        """No division by zero on a question with no time to answer it."""
        self.assertEqual(speed_bonus_for(0.0, 0, rule()), FIFTY_SECONDS)

    def test_the_bonus_is_a_whole_number_of_points(self) -> None:
        # Points are integers on the model, so a fractional bonus would be
        # rounded somewhere; asserting it here pins that rounding to one place.
        for elapsed in (0.0, 0.3, 1.7, 7.3, 13.9, 29.9):
            bonus = speed_bonus_for(elapsed, 30, rule())
            self.assertIsInstance(bonus, int)


class ScoreAnswerTests(SimpleTestCase):
    """The full calculation for one answer."""

    def test_a_correct_answer_scores_the_base_points_and_a_bonus(self) -> None:
        result = score_answer(
            is_correct=True,
            response_time_seconds=10.0,
            duration_seconds=30,
            rule=rule(),
        )

        self.assertTrue(result.is_correct)
        self.assertEqual(result.points, 100)
        # 10s in on a 30s question leaves two thirds of the bonus.
        self.assertEqual(result.speed_bonus, 33)
        self.assertEqual(result.total, 133)

    def test_an_incorrect_answer_scores_nothing_at_all(self) -> None:
        result = score_answer(
            is_correct=False,
            response_time_seconds=1.0,
            duration_seconds=30,
            rule=rule(),
        )

        self.assertFalse(result.is_correct)
        self.assertEqual(result.points, 0)
        self.assertEqual(result.speed_bonus, 0)
        self.assertEqual(result.total, 0)

    def test_a_fast_wrong_answer_still_scores_nothing(self) -> None:
        """Guessing fast must not be worth anything.

        Paying a speed bonus for a wrong answer would reward the exact
        behaviour a live competition is trying to discourage.
        """
        result = score_answer(
            is_correct=False,
            response_time_seconds=0.0,
            duration_seconds=30,
            rule=rule(),
        )

        self.assertEqual(result.total, 0)

    def test_a_slower_correct_answer_scores_less_than_a_faster_one(self) -> None:
        fast = score_answer(
            is_correct=True, response_time_seconds=5.0, duration_seconds=30, rule=rule()
        )
        slow = score_answer(
            is_correct=True, response_time_seconds=20.0, duration_seconds=30, rule=rule()
        )

        self.assertGreater(fast.total, slow.total)
        self.assertEqual(fast.points, slow.points)

    def test_the_configured_correct_points_are_used(self) -> None:
        result = score_answer(
            is_correct=True,
            response_time_seconds=30.0,
            duration_seconds=30,
            rule=rule(correct_points=250),
        )

        self.assertEqual(result.points, 250)
        self.assertEqual(result.total, 250)


@override_settings(
    SCORING_CORRECT_POINTS=42,
    SCORING_SPEED_BONUS_MAX=8,
    SCORING_SPEED_BONUS_MODE=SpeedBonusMode.NONE,
)
class ConfiguredDefaultsTests(SimpleTestCase):
    """The school-wide defaults reach a competition with no rule of its own."""

    def test_defaults_come_from_settings(self) -> None:
        from scoring.models import ScoringRule

        defaults = ScoringRule.defaults()

        self.assertEqual(defaults.correct_points, 42)
        self.assertEqual(defaults.speed_bonus_max, 8)
        self.assertEqual(defaults.speed_bonus_mode, SpeedBonusMode.NONE)

    def test_the_configured_default_scores_an_answer(self) -> None:
        from scoring.models import ScoringRule

        result = score_answer(
            is_correct=True,
            response_time_seconds=1.0,
            duration_seconds=30,
            rule=ScoringRule.defaults(),
        )

        self.assertEqual(result.points, 42)
        self.assertEqual(result.speed_bonus, 0)


class ExtractSelectionTests(SimpleTestCase):
    """Only the chosen answer is ever read out of a client's message."""

    def test_the_answer_key_is_read(self) -> None:
        self.assertEqual(extract_selection({"answer": "4"}), "4")

    def test_the_alternative_key_is_accepted(self) -> None:
        self.assertEqual(extract_selection({"selected_answer": "4"}), "4")

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        self.assertEqual(extract_selection({"answer": "  4 "}), "4")

    def test_a_single_item_list_is_unwrapped(self) -> None:
        self.assertEqual(extract_selection({"answer": ["4"]}), "4")

    def test_a_multi_item_list_is_passed_through(self) -> None:
        # Phase 6: an ordering question is *answered* with a list, so a
        # multi-element list is a legitimate selection and this function must not
        # flatten or reject it. Whether it is structurally acceptable belongs to
        # the open question - see `test_a_multi_item_list_is_refused_by_a_single
        # answer_question`, which is where that check now lives.
        self.assertEqual(
            extract_selection({"answer": ["third", "first", "second"]}),
            ["third", "first", "second"],
        )

    def test_a_mapping_of_placements_is_passed_through(self) -> None:
        self.assertEqual(
            extract_selection({"answer": {"ice": "Solid", "water": "Liquid"}}),
            {"ice": "Solid", "water": "Liquid"},
        )

    def test_a_mixed_list_is_refused(self) -> None:
        with self.assertRaises(AnswerRejected) as caught:
            extract_selection({"answer": ["ice", {"label": "water"}]})

        self.assertEqual(caught.exception.code, "invalid_answer")

    def test_a_mapping_without_a_category_is_refused(self) -> None:
        with self.assertRaises(AnswerRejected) as caught:
            extract_selection({"answer": {"ice": ""}})

        self.assertEqual(caught.exception.code, "invalid_answer")

    def test_a_missing_answer_is_refused(self) -> None:
        with self.assertRaises(AnswerRejected) as caught:
            extract_selection({"type": "submit_answer"})

        self.assertEqual(caught.exception.code, "missing_answer")

    def test_a_blank_answer_is_refused(self) -> None:
        with self.assertRaises(AnswerRejected):
            extract_selection({"answer": "   "})

    def test_a_boolean_is_read_as_true_or_false(self) -> None:
        self.assertEqual(extract_selection({"answer": True}), "true")
        self.assertEqual(extract_selection({"answer": False}), "false")