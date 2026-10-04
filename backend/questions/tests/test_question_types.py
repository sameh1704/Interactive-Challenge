"""Ordering, classification and short answer questions.

Three questions are asked of every type here:

1. Does it validate? A question whose stored data contradicts its type must be
   refused, or it reaches a classroom mid-lesson and strands it.
2. Does it score correctly? Correctness is decided on the server against the
   stored key, including the normalised comparisons a short answer needs.
3. Does it leak? The payload a screen receives while a question is open must
   contain everything needed to draw the question and nothing that would tell a
   class the answer.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from core.tests.utils import (
    create_classification_question,
    create_ordering_question,
    create_question,
    create_short_answer_question,
    create_true_false_question,
)
from questions.models import QuestionType
from questions.normalisation import normalise_short_answer


def key_free(payload: dict) -> bool:
    """Whether a live payload leaks nothing it should not.

    Checked by key name and by value, because a structured key could hide inside
    a list or a nested object where a top-level name check would miss it.
    """
    forbidden = {
        "correct_option",
        "correct_answer",
        "accepted_answers",
        "correct_order",
        "assignments",
        "explanation",
        "key",
    }
    return not forbidden.intersection(payload.keys())


class OrderingQuestionTests(TestCase):
    def setUp(self) -> None:
        self.question = create_ordering_question(
            items=["seed", "sprout", "tree"],
            correct_order=["seed", "sprout", "tree"],
        )

    def test_it_validates(self) -> None:
        self.question.full_clean()

    def test_it_is_scorable(self) -> None:
        self.assertTrue(self.question.has_answer_key)

    def test_the_key_is_stored_and_readable(self) -> None:
        self.assertEqual(self.question.answer_key, "seed > sprout > tree")

    def test_the_right_order_matches(self) -> None:
        self.assertTrue(
            self.question.matches(["seed", "sprout", "tree"])
        )

    def test_a_swapped_order_does_not_match(self) -> None:
        self.assertFalse(self.question.matches(["sprout", "seed", "tree"]))

    def test_a_partial_order_is_refused(self) -> None:
        self.assertFalse(self.question.accepts(["seed", "sprout"]))

    def test_a_repeated_item_is_refused(self) -> None:
        # A duplicate would let a screen pad its answer to look longer.
        self.assertFalse(
            self.question.accepts(["seed", "seed", "tree"])
        )

    def test_an_item_that_was_never_offered_is_refused(self) -> None:
        self.assertFalse(
            self.question.accepts(["seed", "sprout", "bush"])
        )

    def test_a_non_list_is_refused(self) -> None:
        self.assertFalse(self.question.accepts("seed, sprout, tree"))

    def test_the_order_is_a_permutation_of_the_items(self) -> None:
        broken = create_ordering_question(
            items=["seed", "sprout", "tree"],
            correct_order=["seed", "sprout"],
        )
        with self.assertRaises(ValidationError) as caught:
            broken.full_clean()

        self.assertIn("type_config", caught.exception.message_dict)

    def test_duplicate_items_are_refused(self) -> None:
        broken = create_ordering_question(
            items=["seed", "seed", "tree"],
            correct_order=["seed", "tree", "seed"],
        )
        with self.assertRaises(ValidationError):
            broken.full_clean()

    def test_the_payload_carries_the_items_and_no_key(self) -> None:
        payload = self.question.as_live_payload(
            number=1, total=3, duration_seconds=30
        )

        self.assertEqual(payload["question_type"], QuestionType.ORDERING)
        self.assertEqual(
            payload["presentation"]["items"], ["seed", "sprout", "tree"]
        )
        self.assertTrue(key_free(payload))
        self.assertNotIn("correct_order", str(payload))


class ClassificationQuestionTests(TestCase):
    def setUp(self) -> None:
        self.question = create_classification_question(
            categories=["Solid", "Liquid"],
            assignments=[("ice", "Solid"), ("water", "Liquid")],
        )

    def test_it_validates(self) -> None:
        self.question.full_clean()

    def test_the_key_lists_every_placement(self) -> None:
        self.assertEqual(self.question.answer_key, "ice = Solid, water = Liquid")

    def test_the_right_placements_match(self) -> None:
        self.assertTrue(
            self.question.matches({"ice": "Solid", "water": "Liquid"})
        )

    def test_a_list_of_placements_matches_too(self) -> None:
        # The screen page and a script naturally use different shapes; both are
        # accepted so neither has to translate for the other.
        self.assertTrue(
            self.question.matches(
                [{"label": "ice", "category": "Solid"},
                 {"label": "water", "category": "Liquid"}]
            )
        )

    def test_one_wrong_placement_does_not_match(self) -> None:
        self.assertFalse(
            self.question.matches({"ice": "Liquid", "water": "Liquid"})
        )

    def test_a_missing_item_is_refused(self) -> None:
        self.assertFalse(self.question.accepts({"ice": "Solid"}))

    def test_an_unknown_category_is_refused(self) -> None:
        self.assertFalse(
            self.question.accepts({"ice": "Solid", "water": "Gas"})
        )

    def test_a_repeated_item_is_refused(self) -> None:
        # Structurally ambiguous rather than merely wrong: two placements for one
        # item, with no way to say which was meant.
        self.assertFalse(
            self.question.accepts(
                [{"label": "ice", "category": "Solid"},
                 {"label": "ice", "category": "Liquid"},
                 {"label": "water", "category": "Liquid"}]
            )
        )

    def test_a_category_that_was_never_offered_is_refused_when_authoring(self) -> None:
        broken = create_classification_question(
            categories=["Solid"],
            assignments=[("ice", "Solid"), ("water", "Gas")],
        )
        with self.assertRaises(ValidationError) as caught:
            broken.full_clean()

        self.assertIn("type_config", caught.exception.message_dict)

    def test_the_payload_carries_the_items_and_categories_but_no_key(self) -> None:
        payload = self.question.as_live_payload(
            number=2, total=3, duration_seconds=30
        )

        self.assertEqual(
            payload["presentation"]["categories"], ["Solid", "Liquid"]
        )
        self.assertEqual(payload["presentation"]["items"], ["ice", "water"])
        self.assertTrue(key_free(payload))


class ShortAnswerQuestionTests(TestCase):
    def setUp(self) -> None:
        self.question = create_short_answer_question(
            accepted_answers=["photosynthesis"]
        )

    def test_it_validates(self) -> None:
        self.question.full_clean()

    def test_the_key_is_the_teacher_s_own_wording(self) -> None:
        self.assertEqual(self.question.answer_key, "photosynthesis")

    def test_the_exact_answer_matches(self) -> None:
        self.assertTrue(self.question.matches("photosynthesis"))

    def test_surrounding_spaces_are_ignored(self) -> None:
        self.assertTrue(self.question.matches("  photosynthesis  "))

    def test_repeated_internal_spaces_are_ignored(self) -> None:
        question = create_short_answer_question(
            accepted_answers=["the photosynthesis process"]
        )

        self.assertTrue(question.matches("the   photosynthesis   process"))

    def test_extra_words_are_not_ignored(self) -> None:
        # Collapsing whitespace is not the same as ignoring words: the stored key
        # is the whole answer, not a phrase containing it.
        self.assertFalse(self.question.matches("the photosynthesis process"))

    def test_case_is_ignored_by_default(self) -> None:
        self.assertTrue(self.question.matches("PhotoSynthesis"))

    def test_a_non_breaking_space_is_treated_as_a_space(self) -> None:
        # Teachers paste from documents full of them and a touchscreen keyboard
        # can produce one by accident. Both are stored as a plain space, so the
        # two spellings are one answer.
        question = create_short_answer_question(
            accepted_answers=["the photosynthesis process"]
        )

        self.assertTrue(question.matches("the photosynthesis process"))

    def test_a_different_word_does_not_match(self) -> None:
        # No fuzzy matching: "photosynthesising" is a different word, and
        # accepting it would mark a pupil right who has not understood it.
        self.assertFalse(self.question.matches("photosynthesising"))

    def test_a_typo_does_not_match(self) -> None:
        self.assertFalse(self.question.matches("photosynthesys"))

    def test_a_blank_answer_is_refused(self) -> None:
        self.assertFalse(self.question.accepts("   "))

    def test_a_case_sensitive_question_is_exact(self) -> None:
        question = create_short_answer_question(
            accepted_answers=["Paris"], case_sensitive=True
        )

        self.assertTrue(question.matches("Paris"))
        self.assertFalse(question.matches("paris"))

    def test_a_second_accepted_answer_is_allowed(self) -> None:
        question = create_short_answer_question(
            accepted_answers=["photosynthesis", "light reaction"]
        )

        self.assertTrue(question.matches("light reaction"))
        self.assertFalse(question.matches("respiration"))

    def test_the_key_displayed_is_the_first_accepted_answer(self) -> None:
        question = create_short_answer_question(
            accepted_answers=["light reaction", "photosynthesis"]
        )

        self.assertEqual(question.answer_key, "light reaction")

    def test_a_question_with_no_accepted_answers_is_not_scorable(self) -> None:
        question = create_short_answer_question(accepted_answers=[])

        self.assertFalse(question.has_answer_key)
        self.assertIsNone(question.answer_key)

    def test_the_payload_carries_a_length_hint_and_no_key(self) -> None:
        payload = self.question.as_live_payload(
            number=1, total=1, duration_seconds=30
        )

        self.assertEqual(payload["presentation"]["max_length"], len("photosynthesis"))
        self.assertTrue(key_free(payload))


class NormalisationTests(SimpleTestCase):
    """The comparison rules on their own, away from any model."""

    def test_tabs_and_newlines_collapse(self) -> None:
        self.assertEqual(normalise_short_answer("a\tb\nc"), "a b c")

    def test_casefold_handles_a_sharp_s(self) -> None:
        self.assertEqual(normalise_short_answer("STRASSE"), "strasse")

    def test_accents_are_not_stripped(self) -> None:
        # "café" and "cafe" are different words.
        self.assertNotEqual(normalise_short_answer("café"), normalise_short_answer("cafe"))


class BackwardCompatibilityTests(TestCase):
    """The types that existed before this phase must behave exactly as they did."""

    def test_a_multiple_choice_question_is_unchanged(self) -> None:
        question = create_question(options=["3", "4", "5", "6"], correct_option="4")

        question.full_clean()

        self.assertEqual(question.answer_key, "4")
        self.assertTrue(question.matches("4"))
        self.assertFalse(question.matches("5"))
        self.assertEqual(question.accepts("4"), True)
        self.assertEqual(question.accepts("42"), False)

    def test_a_true_false_question_is_unchanged(self) -> None:
        question = create_true_false_question(value=True)

        question.full_clean()

        self.assertEqual(question.answer_key, "true")
        self.assertTrue(question.matches("true"))
        self.assertTrue(question.matches("TRUE"))
        self.assertFalse(question.matches("false"))

    def test_the_live_payload_of_an_old_question_is_unchanged(self) -> None:
        question = create_question(options=["3", "4"], correct_option="4")

        payload = question.as_live_payload(number=1, total=1, duration_seconds=30)

        self.assertEqual(payload["options"], ["3", "4"])
        self.assertEqual(payload["presentation"], {})
        self.assertTrue(key_free(payload))


class ExplanationTests(TestCase):
    def test_an_explanation_is_not_sent_while_the_question_is_open(self) -> None:
        question = create_question(
            options=["3", "4"],
            correct_option="4",
            explanation="Because 2 and 2 make 4.",
        )

        payload = question.as_live_payload(number=1, total=1, duration_seconds=30)

        self.assertTrue(key_free(payload))
        self.assertNotIn("explanation", str(payload))

    def test_an_explanation_is_blank_by_default(self) -> None:
        self.assertEqual(create_question().explanation, "")


class QuestionImageTests(TestCase):
    """A question may carry an image, but only a real image.

    Field validators run during ``full_clean()``, which is where the admin form
    and any programmatic save go, so that is what these tests exercise.
    """

    def test_a_png_is_accepted(self) -> None:
        question = create_question(options=["3", "4"], correct_option="4")
        question.image = "questions/diagram.png"

        question.full_clean()

        self.assertEqual(question.image.name, "questions/diagram.png")

    def test_a_script_is_refused(self) -> None:
        question = create_question(options=["3", "4"], correct_option="4")
        question.image = "questions/notes.js"

        with self.assertRaises(ValidationError) as caught:
            question.full_clean()

        self.assertIn("image", caught.exception.message_dict)

    def test_an_html_file_is_refused(self) -> None:
        question = create_question(options=["3", "4"], correct_option="4")
        question.image = "questions/page.html"

        with self.assertRaises(ValidationError):
            question.full_clean()

    def test_a_pdf_is_refused(self) -> None:
        question = create_question(options=["3", "4"], correct_option="4")
        question.image = "questions/worksheet.pdf"

        with self.assertRaises(ValidationError):
            question.full_clean()

    def test_an_executable_is_refused(self) -> None:
        question = create_question(options=["3", "4"], correct_option="4")
        question.image = "questions/run.exe"

        with self.assertRaises(ValidationError):
            question.full_clean()

    def test_the_image_url_appears_only_when_there_is_one(self) -> None:
        without = create_question(options=["3", "4"], correct_option="4")
        self.assertIsNone(
            without.as_live_payload(number=1, total=1, duration_seconds=30)["image_url"]
        )

        with_image = create_question(options=["3", "4"], correct_option="4")
        with_image.image = "questions/diagram.jpg"
        self.assertTrue(
            with_image.as_live_payload(number=1, total=1, duration_seconds=30)["image_url"]
        )