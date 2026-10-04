"""Answers, scoring rules and results.

Why this is its own app
-----------------------
Scoring is the one part of the system where a wrong answer is a wrong answer for
*ever*: it goes on the record and the leaderboard is derived from it. So it is
kept apart from the live engine, which is concerned only with moving a round
between states.

The split is also what makes the phase brief's first requirement true - scoring
is independent of the UI. :mod:`scoring.services` is plain synchronous Python
over the database: it has no knowledge of WebSockets, HTTP or templates, and
every rule it applies is callable from a test without a browser.

What lives here
---------------
``Answer``
    One classroom's final answer to one question in one round. Written once,
    never edited.

``ScoringRule``
    The configurable part: points for a correct answer, and how much of the
    speed bonus is available. Per competition, falling back to settings.

``CompetitionResult``
    An immutable snapshot of the final standings, taken when the round finishes.

The authority rule
------------------
Everything that decides an outcome is derived here, on the server, from the
stored data:

* **which classroom answered** - from the authenticated screen connection, never
  from the message;
* **when it answered** - from the server clock at the moment of receipt, never
  from a timestamp the client supplied;
* **whether it was correct** - from the question's stored key, never from the
  client;
* **what it scored** - from the stored rule, never from the client.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# A speed bonus that could outweigh the answer itself would reward guessing over
# knowing. The ceiling keeps a correct answer worth at most 1.5x a correct one.
MAX_CORRECT_POINTS = 1000
MAX_SPEED_BONUS_POINTS = 500

# Recorded on an answer so that a report can explain it without re-deriving.
# A client can influence neither.
DEFAULT_CORRECT_POINTS = 100
DEFAULT_SPEED_BONUS_POINTS = 50


class SpeedBonusMode(models.TextChoices):
    """How the speed bonus is earned.

    ``LINEAR``
        The full bonus is available immediately and decays to zero by the end of
        the question, so answering early is worth more.

    ``NONE``
        No speed bonus at all, whatever ``speed_bonus_max`` says. Kept as an
        explicit mode so a teacher can switch it off without losing the value.
    """

    LINEAR = "linear", _("Linear decay")
    NONE = "none", _("No speed bonus")


class ScoringRuleQuerySet(models.QuerySet):
    def for_competition(self, competition):
        return self.filter(competition=competition)


class ScoringRule(models.Model):
    """How points are awarded in one competition.

    A competition with no rule uses the defaults from settings, so scoring works
    out of the box and a school can adjust the numbers without touching code.
    """

    competition = models.OneToOneField(
        "competitions.Competition",
        on_delete=models.CASCADE,
        related_name="scoring_rule",
        help_text="The round this rule applies to.",
    )
    correct_points = models.PositiveIntegerField(
        default=DEFAULT_CORRECT_POINTS,
        validators=[MaxValueValidator(MAX_CORRECT_POINTS)],
        help_text="Points awarded for a correct answer. An incorrect answer scores nothing.",
    )
    speed_bonus_max = models.PositiveIntegerField(
        default=DEFAULT_SPEED_BONUS_POINTS,
        validators=[MaxValueValidator(MAX_SPEED_BONUS_POINTS)],
        help_text=(
            "Most extra points available for answering quickly. Set to 0, or "
            "switch the mode to 'No speed bonus', to disable the bonus."
        ),
    )
    speed_bonus_mode = models.CharField(
        max_length=16,
        choices=SpeedBonusMode.choices,
        default=SpeedBonusMode.LINEAR,
        help_text="How the bonus is earned as the answer period runs down.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ScoringRuleQuerySet.as_manager()

    class Meta:
        verbose_name = "scoring rule"
        verbose_name_plural = "scoring rules"
        ordering = ["competition_id"]

    def __str__(self) -> str:
        return f"Scoring for {self.competition_id}"

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.correct_points < 0:
            errors["correct_points"] = "Points cannot be negative."
        if self.correct_points > MAX_CORRECT_POINTS:
            errors["correct_points"] = (
                f"Points cannot exceed {MAX_CORRECT_POINTS}."
            )
        if self.speed_bonus_max < 0:
            errors["speed_bonus_max"] = "The bonus cannot be negative."
        if self.speed_bonus_max > MAX_SPEED_BONUS_POINTS:
            errors["speed_bonus_max"] = (
                f"The bonus cannot exceed {MAX_SPEED_BONUS_POINTS}."
            )
        if errors:
            raise ValidationError(errors)

    @classmethod
    def defaults(cls) -> "ScoringRuleValues":
        """The configured defaults, used when a competition has no rule row."""
        from django.conf import settings

        return ScoringRuleValues(
            correct_points=getattr(
                settings, "SCORING_CORRECT_POINTS", DEFAULT_CORRECT_POINTS
            ),
            speed_bonus_max=getattr(
                settings, "SCORING_SPEED_BONUS_MAX", DEFAULT_SPEED_BONUS_POINTS
            ),
            speed_bonus_mode=getattr(
                settings, "SCORING_SPEED_BONUS_MODE", SpeedBonusMode.LINEAR
            ),
        )


class ScoringRuleValues:
    """A scoring configuration that need not be a saved row.

    Lets :func:`scoring.services.score_answer` be a pure function of
    (rule values, correctness, elapsed, duration) with no database lookup, so the
    arithmetic is testable on its own.
    """

    __slots__ = ("correct_points", "speed_bonus_max", "speed_bonus_mode")

    def __init__(
        self,
        correct_points: int = DEFAULT_CORRECT_POINTS,
        speed_bonus_max: int = DEFAULT_SPEED_BONUS_POINTS,
        speed_bonus_mode: str = SpeedBonusMode.LINEAR,
    ):
        self.correct_points = int(correct_points)
        self.speed_bonus_max = int(speed_bonus_max)
        self.speed_bonus_mode = speed_bonus_mode

    def __eq__(self, other) -> bool:
        if not isinstance(other, ScoringRuleValues):
            return NotImplemented
        return (
            self.correct_points == other.correct_points
            and self.speed_bonus_max == other.speed_bonus_max
            and self.speed_bonus_mode == other.speed_bonus_mode
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ScoringRuleValues(correct_points={self.correct_points}, "
            f"speed_bonus_max={self.speed_bonus_max}, "
            f"speed_bonus_mode={self.speed_bonus_mode!r})"
        )


class AnswerQuerySet(models.QuerySet):
    def for_competition(self, competition):
        return self.filter(competition=competition)

    def correct(self):
        return self.filter(is_correct=True)

    def by_position(self):
        return self.order_by("position", "classroom__name")


class Answer(models.Model):
    """One classroom's final answer to one question of one competition.

    Written exactly once. There is no update path and no manager method that
    changes an existing answer, because a stored answer is a historical fact: if
    it could be edited, the leaderboard would stop being reproducible and a
    report could not be trusted.
    """

    competition = models.ForeignKey(
        "competitions.Competition",
        on_delete=models.CASCADE,
        related_name="answers",
        help_text="The round this answer belongs to.",
    )
    question = models.ForeignKey(
        "questions.Question",
        on_delete=models.PROTECT,
        related_name="answers",
        help_text="The question that was asked.",
    )
    position = models.PositiveIntegerField(
        help_text="1-based position of the question within the round, copied for reporting.",
    )
    classroom = models.ForeignKey(
        "classrooms.Classroom",
        on_delete=models.PROTECT,
        related_name="answers",
        help_text="The classroom that answered. Taken from the screen's own connection.",
    )
    screen = models.ForeignKey(
        "screens.InteractiveScreen",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answers",
        help_text=(
            "The screen the answer arrived on. Kept for network diagnosis; a "
            "screen being retired later must not erase the answer."
        ),
    )
    selected_answer = models.JSONField(
        help_text="Exactly what the classroom chose, as submitted.",
    )
    submitted_at = models.DateTimeField(
        default=timezone.now,
        help_text="Server time at which the answer was received.",
    )
    response_time_seconds = models.FloatField(
        help_text=(
            "Seconds from the server starting the question to receiving this "
            "answer. Computed on the server from its own clock."
        ),
    )
    duration_seconds = models.PositiveIntegerField(
        help_text="The time allowed for this question, copied for reporting.",
    )
    is_correct = models.BooleanField(
        help_text="Whether the selection matched the stored answer key.",
    )
    points = models.PositiveIntegerField(
        default=0,
        help_text="Points for correctness alone, before any bonus.",
    )
    speed_bonus = models.PositiveIntegerField(
        default=0,
        help_text="Bonus points for answering quickly.",
    )
    correct_answer = models.CharField(
        max_length=1000,
        blank=True,
        help_text=(
            "The answer key as it stood when this was scored, kept so a report "
            "can explain the outcome even if the question is later edited. Sized "
            "for the longest key an ordering or classification question can "
            "produce; see `questions.models` for the item bounds."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AnswerQuerySet.as_manager()

    class Meta:
        verbose_name = "answer"
        verbose_name_plural = "answers"
        ordering = ["competition_id", "position", "classroom__name"]
        indexes = [
            models.Index(
                fields=["competition", "position"],
                name="scoring_answer_by_question_idx",
            ),
        ]
        constraints = [
            # The guarantee behind "one final answer per question per classroom".
            # Enforced by the database rather than only by the service, so two
            # simultaneous submissions cannot both succeed - a check in Python
            # alone would still race, and this is exactly the case where a race
            # would let one classroom answer twice and out-score the others.
            models.UniqueConstraint(
                fields=["competition", "question", "classroom"],
                name="scoring_one_answer_per_classroom_question",
            )
        ]

    def __str__(self) -> str:
        return f"{self.classroom} answered {self.selected_answer!r}"

    @property
    def total_score(self) -> int:
        return self.points + self.speed_bonus

    @property
    def selection(self) -> str:
        """The selected answer as a plain string, for display and reporting.

        An ordering answer is stored as a sequence and a classification answer as
        a mapping of item to category, because that is the answer itself and
        flattening it at write time would lose information the report needs. They
        are rendered here instead, so the stored value stays faithful.
        """
        value = self.selected_answer
        if isinstance(value, list):
            return " → ".join(str(item) for item in value)
        if isinstance(value, dict):
            return ", ".join(
                f"{label} = {category}" for label, category in value.items()
            )
        return str(value)

    @property
    def was_late(self) -> bool:
        """Whether the answer arrived after its question's deadline."""
        return self.response_time_seconds > self.duration_seconds


class CompetitionResult(models.Model):
    """The final standings of a round, frozen when it finished.

    A snapshot rather than a view. Once a round is over its standings should not
    move: a later correction to an answer is a new fact about history, not a
    reason to silently rewrite the result a teacher announced to a room full of
    students. Reports read this row; the live leaderboard reads live data.
    """

    competition = models.OneToOneField(
        "competitions.Competition",
        on_delete=models.CASCADE,
        related_name="result",
        help_text="The round these standings belong to.",
    )
    standings = models.JSONField(
        default=list,
        help_text=(
            "Ranked rows as they stood at the finish: rank, classroom, score, "
            "correct answers, answers given."
        ),
    )
    total_questions = models.PositiveIntegerField(
        default=0,
        help_text="Questions asked in the round.",
    )
    answered_count = models.PositiveIntegerField(
        default=0,
        help_text="Answers received across the round.",
    )
    winner_name = models.CharField(
        max_length=160,
        blank=True,
        help_text="Leading classroom, if any. Blank when the round had no answers.",
    )
    generated_at = models.DateTimeField(
        default=timezone.now,
        help_text="When this snapshot was taken.",
    )

    class Meta:
        verbose_name = "competition result"
        verbose_name_plural = "competition results"
        ordering = ["-generated_at"]

    def __str__(self) -> str:
        return f"Result: {self.competition_id}"

    @property
    def rows(self) -> list:
        return list(self.standings or [])

    def row_for(self, classroom_id: int) -> dict | None:
        for row in self.rows:
            if row.get("classroom_id") == classroom_id:
                return row
        return None


__all__ = [
    "Answer",
    "CompetitionResult",
    "DEFAULT_CORRECT_POINTS",
    "DEFAULT_SPEED_BONUS_POINTS",
    "MAX_CORRECT_POINTS",
    "MAX_SPEED_BONUS_POINTS",
    "ScoringRule",
    "ScoringRuleValues",
    "SpeedBonusMode",
]