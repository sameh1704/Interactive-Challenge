"""Competition models: who is playing, what they are playing, and where.

Scope for this phase
--------------------
Phase 4 needs three things persisted so that state survives a teacher refreshing
their dashboard or a screen reconnecting mid-round:

* ``Competition``     - the round, its owner, and its current state.
* ``CompetitionClassroom`` - which classrooms were included. This is the
  authorisation boundary: a registered screen may only take part if its classroom
  appears here.
* ``CompetitionQuestion`` - the ordered questions of this round.

Scores are **not** modelled here. Rule 14 of the project says not to build ahead,
and the phase brief says not to compute final scores yet; a half-built answer
model would be worse than none because it would fix the shape of scoring before
the school has decided how scoring should work.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class CompetitionState(models.TextChoices):
    """The lifecycle of a live round.

    Only two transitions are automatic, and both are driven by the server clock
    rather than by any browser:

    ``QUESTION_ACTIVE`` -> ``ANSWERING_CLOSED``
        when the question's duration has elapsed.

    ``ANSWERING_CLOSED`` -> ``SHOWING_RESULT``
        when the teacher advances.

    Everything else is an explicit teacher action. A screen never moves the
    state, and a browser's local clock is never trusted to move it.
    """

    WAITING = "waiting", _("Waiting")
    QUESTION_ACTIVE = "question_active", _("Question active")
    ANSWERING_CLOSED = "answering_closed", _("Answering closed")
    SHOWING_RESULT = "showing_result", _("Showing result")
    NEXT_QUESTION = "next_question", _("Next question")
    FINISHED = "finished", _("Finished")


# States in which a competition is still running and screens may be driven.
ACTIVE_STATES = frozenset(
    {
        CompetitionState.WAITING,
        CompetitionState.QUESTION_ACTIVE,
        CompetitionState.ANSWERING_CLOSED,
        CompetitionState.SHOWING_RESULT,
        CompetitionState.NEXT_QUESTION,
    }
)


class CompetitionQuerySet(models.QuerySet):
    def active(self):
        """Rounds that are not finished."""
        return self.filter(state__in=list(ACTIVE_STATES))

    def finished(self):
        return self.filter(state=CompetitionState.FINISHED)

    def for_user(self, user):
        """Competitions a staff member may run.

        A teacher runs the rounds they started; an administrator sees every
        round, because they can be asked to take over one.
        """
        from accounts.permissions import is_administrator

        if not user or not user.is_authenticated or not user.is_active:
            return self.none()
        if is_administrator(user):
            return self
        return self.filter(teacher=user)


class Competition(models.Model):
    """One live round between a set of classrooms."""

    title = models.CharField(
        max_length=160,
        help_text="For example 'Science Lab 1 versus Science Lab 2'.",
    )
    teacher = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="competitions",
        help_text="The member of staff running this round.",
    )
    state = models.CharField(
        max_length=32,
        choices=CompetitionState.choices,
        default=CompetitionState.WAITING,
        db_index=True,
        help_text="Where the round has reached. Advanced by the server or the teacher.",
    )
    current_question = models.ForeignKey(
        "questions.Question",
        on_delete=models.PROTECT,
        related_name="current_in_competitions",
        null=True,
        blank=True,
        help_text="The question currently on screen, if any.",
    )
    current_question_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Server time at which the current question began. This, not a "
            "browser clock, is what the countdown is derived from."
        ),
    )
    current_question_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Server time at which answering closes for the current question.",
    )
    question_number = models.PositiveIntegerField(
        default=0,
        help_text="1-based position of the current question within the round.",
    )
    total_questions = models.PositiveIntegerField(
        default=0,
        help_text="Number of questions in this round.",
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set once, when the teacher starts the round.",
    )
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CompetitionQuerySet.as_manager()

    class Meta:
        verbose_name = "competition"
        verbose_name_plural = "competitions"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    # -- membership --------------------------------------------------------

    @property
    def participating_classroom_ids(self) -> set[int]:
        return set(
            self.classrooms.values_list("classroom_id", flat=True).order_by()
        )

    def includes_classroom(self, classroom_id: int | None) -> bool:
        """Whether a classroom was included when this round was set up.

        This is the authorisation check used by the live engine. It is a
        database read rather than a cache, because a screen must not be able to
        join by acting on a stale allow-list after an administrator has removed
        a classroom.
        """
        if classroom_id is None:
            return False
        return self.classrooms.filter(classroom_id=classroom_id).exists()

    # -- state -------------------------------------------------------------

    @property
    def is_finished(self) -> bool:
        return self.state == CompetitionState.FINISHED

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    def seconds_remaining(self, now=None) -> float:
        """Seconds left on the current question, per the server clock.

        Screens call this (directly or through a broadcast) rather than counting
        down locally, so that every screen in the school shows the same number.
        """
        if self.current_question_ends_at is None:
            return 0.0
        now = now or timezone.now()
        return max(0.0, (self.current_question_ends_at - now).total_seconds())

    def clean(self) -> None:
        super().clean()
        if self.current_question_id and self.question_number < 1:
            raise ValidationError(
                {
                    "question_number": (
                        "A competition showing a question must have a question "
                        "number of at least 1."
                    )
                }
            )
        if (
            self.current_question_ends_at
            and self.current_question_started_at
            and self.current_question_ends_at <= self.current_question_started_at
        ):
            raise ValidationError(
                {
                    "current_question_ends_at": (
                        "The question must end after it starts."
                    )
                }
            )


class CompetitionClassroom(models.Model):
    """A classroom included in a competition.

    Created when the teacher sets up the round. A registered screen whose
    classroom has no row here is refused by the live engine even though the
    screen itself is perfectly valid.
    """

    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="classrooms",
    )
    classroom = models.ForeignKey(
        "classrooms.Classroom",
        on_delete=models.CASCADE,
        related_name="competition_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "competition classroom"
        verbose_name_plural = "competition classrooms"
        ordering = ["classroom__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "classroom"],
                name="competitions_one_entry_per_classroom",
            )
        ]

    def __str__(self) -> str:
        return f"{self.classroom} in {self.competition}"


class CompetitionQuestion(models.Model):
    """One question in a round, in the order it will be asked."""

    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="questions",
    )
    question = models.ForeignKey(
        "questions.Question",
        on_delete=models.PROTECT,
        related_name="competition_entries",
    )
    position = models.PositiveIntegerField(
        help_text="1-based order in which this question is asked.",
    )
    duration_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Overrides the question's own duration for this round. Leave blank "
            "to use the question's default."
        ),
    )

    class Meta:
        verbose_name = "competition question"
        verbose_name_plural = "competition questions"
        ordering = ["competition", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "position"],
                name="competitions_one_question_per_position",
            ),
            # A question must not appear twice in one round, or the "which
            # question is next" lookup becomes ambiguous.
            models.UniqueConstraint(
                fields=["competition", "question"],
                name="competitions_question_appears_once",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.position}. {self.question}"

    @property
    def effective_duration_seconds(self) -> int:
        return self.duration_seconds or self.question.duration_seconds
