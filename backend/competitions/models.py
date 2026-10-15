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


def default_points_for(competition_id: int | None = None) -> int:
    """The default points a correct answer earns in a round.

    Prefers the competition's scoring rule when it exists, otherwise falls back
    to the configured defaults. A local import keeps scoring and competitions
    from forming a hard circular import at module load time.
    """
    try:
        from scoring.models import DEFAULT_CORRECT_POINTS, ScoringRule
    except Exception:  # noqa: BLE001 - safe fallback if scoring not ready
        return 100

    if competition_id is not None:
        try:
            rule = ScoringRule.objects.filter(competition_id=competition_id).first()
            if rule is not None:
                return rule.correct_points
        except Exception:  # noqa: BLE001 - DB access should not break property
            pass

    try:
        from django.conf import settings

        return getattr(settings, "SCORING_CORRECT_POINTS", DEFAULT_CORRECT_POINTS)
    except Exception:  # noqa: BLE001 - safe fallback
        return DEFAULT_CORRECT_POINTS


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
        if user.is_stage_deputy:
            from accounts.stage_scope import competitions_for_deputy
            return competitions_for_deputy(user, queryset=self)
        if not user.is_teacher:
            return self.none()
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
    # When the teacher *intends* to run this competition.
    #
    # Deliberately not a start trigger: it is a note to themselves and to the
    # competition list, and nothing in the system reads it to change state. A
    # competition that is live is the one whose `started_at` is set - which only
    # the teacher's own Start button does. Keeping the two apart is what makes
    # "planned for 10:30" honest: it says when, not "and it started itself".
    planned_for = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the teacher intends to run this competition. Scheduling a "
            "time does not start it; only the Start button does."
        ),
    )
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

    # -- the preparation lifecycle ------------------------------------------
    #
    # Three phases a teacher cares about, derived from timestamps that already
    # exist rather than from a second state field. `state` describes where a
    # *running* round has got to; it has no way to say "prepared but not
    # started", which is the state a teacher spends days in.
    #
    # `started_at` is the one and only marker of "live", and only the teacher's
    # Start button writes it. `planned_for` is a note about intent and is read by
    # nothing that changes state.

    @property
    def is_started(self) -> bool:
        """Whether this competition has ever been started."""
        return self.started_at is not None

    @property
    def is_live(self) -> bool:
        """Whether the round is running now: started, and not yet finished."""
        return self.started_at is not None and self.state != CompetitionState.FINISHED

    @property
    def is_prepared(self) -> bool:
        """Whether the round is prepared and waiting for the teacher to start it."""
        return self.started_at is None

    @property
    def default_points(self) -> int:
        """Points a correct answer earns in this round, before any override."""
        return default_points_for(self.pk)

    @property
    def total_points(self) -> int:
        """The competition's total possible score, added up from its questions.

        Derived from the stored rows and never from a value a browser sent, so
        what the review page showed and what the round awards cannot drift
        apart. A question's own override is used when it has one.
        """
        default = self.default_points
        return sum(entry.points_or(default) for entry in self.questions.all())

    @property
    def lifecycle_label(self) -> str:
        """A one-word phrase for where this competition stands.

        Translated at the call site through the template, not here: the models
        stay free of the request's language.
        """
        if self.is_finished:
            return "finished"
        if self.is_live:
            return "live"
        if self.planned_for is not None:
            return "planned"
        return "prepared"

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
    # What this question is worth in this round. Nullable, and that nullability
    # is the migration path: every row written before per-question points
    # existed has no value here and continues to score the competition-wide
    # default exactly as it did before. A value written from now on overrides
    # that default for this question only - the scoring engine reads this row,
    # so a question the teacher marked as worth five points scores five, not the
    # round's default.
    points = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "What a correct answer to this question is worth in this round. "
            "Leave blank to use the competition's points per question."
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

    @property
    def effective_points(self) -> int:
        """What this question is worth: its own value, or the round's default.

        The fallback is a local import rather than a field, so that a round set
        up before per-question points existed - and one whose teacher never
        opened the scoring step - both score the value the scoring engine would
        have given them anyway.
        """
        if self.points is not None:
            return self.points
        return default_points_for(self.competition_id)

    def points_or(self, default_points: int) -> int:
        """This question's points, with an explicit default for the unknown case.

        The caller resolves the round's default once, which is what keeps a
        review page or a scoring call from doing one lookup per question.
        """
        if self.points is not None:
            return self.points
        return default_points
