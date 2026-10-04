"""Shared helpers for building test data.

A thin re-export of :mod:`core.tests.factories`, so that a test module can
import from one obvious place without caring which file defines the helper.
"""

from core.tests.factories import (
    TEST_PASSWORD,
    add_participants,
    add_question,
    attach_competition,
    complete_competition,
    create_administrator,
    create_answer,
    create_classification_question,
    create_classroom,
    create_competition,
    create_ordering_question,
    create_question,
    create_scoring_rule,
    create_screen,
    create_short_answer_question,
    create_stage,
    create_teacher,
    create_tournament,
    create_true_false_question,
    create_user,
    include_classroom,
    played_competition,
)

__all__ = [
    "TEST_PASSWORD",
    "add_participants",
    "add_question",
    "attach_competition",
    "complete_competition",
    "create_administrator",
    "create_answer",
    "create_classification_question",
    "create_classroom",
    "create_competition",
    "create_ordering_question",
    "create_question",
    "create_scoring_rule",
    "create_screen",
    "create_short_answer_question",
    "create_stage",
    "create_teacher",
    "create_tournament",
    "create_true_false_question",
    "create_user",
    "include_classroom",
    "played_competition",
]