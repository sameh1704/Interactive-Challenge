"""The tournament layer: championships built from ordinary competitions.

Where this sits
---------------
A tournament is *not* a second competition engine. Every round inside it is a
:class:`competitions.models.Competition`, driven by the same live engine, scored by
the same :mod:`scoring` app and published through the same WebSockets. This app
only decides **which** competitions belong to **which** stage, and **which**
classrooms move from one stage to the next. Nothing here re-implements a question,
an answer, a score or a leaderboard.

Why a separate app at all
-------------------------
Because the questions this app asks are different in kind. A competition asks
"who is right right now?". A tournament asks "which rooms are still in it?", and
its answer must survive the competition it came from being closed. Those are
records about history, so they get their own models rather than being bolted onto
:class:`competitions.models.Competition` as more columns.

Nothing is duplicated
---------------------
* Classrooms are the existing :class:`classrooms.models.Classroom`. A tournament
  participant is a *reference* to a classroom, not a new kind of room.
* Scores come from the frozen :class:`scoring.models.CompetitionResult` of each
  competition. No answer is copied into a tournament table.
* Advancement totals are derived from those snapshots on demand and written only
  as an audit record of *which* classrooms advanced, not of the arithmetic.

Historical integrity
--------------------
:meth:`Tournament.finalise` writes a :class:`TournamentRecord` snapshot, mirroring
:class:`scoring.models.CompetitionResult`. A championship result that could be
edited by editing a past competition would be worth nothing to a school, so the
frozen record and the live graph are kept deliberately separate.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from competitions.models import Competition, CompetitionState

class TournamentStatus(models.TextChoices):
    """The lifecycle of a championship."""

    DRAFT = "draft", _("Draft")
    OPEN = "open", _("Open")
    RUNNING = "running", _("Running")
    COMPLETED = "completed", _("Completed")
    CANCELLED = "cancelled", _("Cancelled")


# Statuses in which a tournament may still be changed. Used by the services to
# refuse an edit that would rewrite a result a school has already announced.
MUTABLE_TOURNAMENT_STATUSES = frozenset(
    {TournamentStatus.DRAFT, TournamentStatus.OPEN, TournamentStatus.RUNNING}
)


#: Statuses in which classrooms may still be entered into the championship. A
#: running tournament is excluded on purpose: see `tournaments.services`.
PARTICIPATION_OPEN_STATUSES = frozenset(
    {TournamentStatus.DRAFT, TournamentStatus.OPEN}
)


class TournamentStageType(models.TextChoices):
    """The rounds of a championship.

    Adding a value here is a data change: a tournament stage is stored by this
    value and ordered by an explicit ``order``, so a school that later wants a
    "play-off" round adds it without a redesign.
    """

    QUALIFICATION = "qualification", _("Qualification")
    SEMI_FINAL = "semi_final", _("Semi final")
    FINAL = "final", _("Final")


# The stage after which nothing else can follow, used to decide when a tournament
# may be finalised.
LAST_STAGE_TYPE = TournamentStageType.FINAL


class TournamentStageStatus(models.TextChoices):
    """Where a stage has reached.

    ``PENDING`` and ``READY`` are pre-round states; ``RUNNING`` means a round has
    finished but the stage has not; ``COMPLETED`` means advancement has been
    processed and the stage is closed to editing.
    """

    PENDING = "pending", _("Pending")
    READY = "ready", _("Ready")
    RUNNING = "running", _("Running")
    COMPLETED = "completed", _("Completed")


#: Stages that may still have competitions attached, classrooms added, or their
#: status changed.
OPEN_STAGE_STATUSES = frozenset(
    {
        TournamentStageStatus.PENDING,
        TournamentStageStatus.READY,
        TournamentStageStatus.RUNNING,
    }
)


class TournamentParticipantStatus(models.TextChoices):
    """Where one classroom stands in the championship."""

    ENTERED = "entered", _("Entered")
    QUALIFIED = "qualified", _("Qualified")
    ELIMINATED = "eliminated", _("Eliminated")
    CHAMPION = "champion", _("Champion")
    WITHDRAWN = "withdrawn", _("Withdrawn")


#: Statuses in which a classroom may still be placed into a later stage.
LIVE_PARTICIPANT_STATUSES = frozenset(
    {
        TournamentParticipantStatus.ENTERED,
        TournamentParticipantStatus.QUALIFIED,
    }
)


class AdvancementStatus(models.TextChoices):
    """The outcome of processing one stage's advancement.

    ``TIE`` is a real, persistable outcome rather than an error to be logged and
    forgotten: it is the state a stage is left in when the cut falls between two
    classrooms that are level, and it is what an administrator later resolves.
    """

    PROCESSED = "processed", _("Processed")
    TIE = "tie", _("Needs resolution")


class TournamentQuerySet(models.QuerySet):
    def for_user(self, user):
        """Tournaments a staff member may open.

        An administrator sees every championship, because they are the person who
        gets asked to take one over. A teacher sees the ones they created, which
        mirrors :meth:`competitions.models.CompetitionQuerySet.for_user` so the
        two permission models cannot drift apart.
        """
        from accounts.permissions import is_administrator

        if not user or not user.is_authenticated or not user.is_active:
            return self.none()
        if is_administrator(user):
            return self
        return self.filter(created_by=user)

    def completed(self):
        return self.filter(status=TournamentStatus.COMPLETED)


class Tournament(models.Model):
    """A school-wide championship run across one or more stages."""

    name = models.CharField(
        max_length=160,
        help_text="For example 'Primary Science Championship 2026'.",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional notes for staff, shown on the tournament page.",
    )
    subject = models.CharField(
        max_length=80,
        blank=True,
        db_index=True,
        help_text="For example 'Science'. Free text, so a new subject needs no code change.",
    )
    grade = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="For example 'Grade 5'. Free text, matching Classroom.grade.",
    )
    season = models.CharField(
        max_length=32,
        blank=True,
        db_index=True,
        help_text="For example '2026' or '2025/2026'. Copied into the historical record.",
    )
    status = models.CharField(
        max_length=16,
        choices=TournamentStatus.choices,
        default=TournamentStatus.DRAFT,
        db_index=True,
        help_text="Moved by the tournament services, never by a form field.",
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="tournaments",
        help_text="The member of staff who set this tournament up.",
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TournamentQuerySet.as_manager()

    class Meta:
        verbose_name = "tournament"
        verbose_name_plural = "tournaments"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(start_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="tournaments_end_not_before_start",
            )
        ]

    def __str__(self) -> str:
        return self.name

    # -- state -------------------------------------------------------------

    @property
    def is_completed(self) -> bool:
        return self.status == TournamentStatus.COMPLETED

    @property
    def is_cancelled(self) -> bool:
        return self.status == TournamentStatus.CANCELLED

    @property
    def is_mutable(self) -> bool:
        """Whether the tournament may still be edited.

        Completed and cancelled championships are history: adding a participant or
        a stage to one would silently change a result that has been published.
        """
        return self.status in MUTABLE_TOURNAMENT_STATUSES

    def clean(self) -> None:
        super().clean()
        errors = {}
        if (
            self.start_date
            and self.end_date
            and self.end_date < self.start_date
        ):
            errors["end_date"] = "The end date cannot be before the start date."
        if errors:
            raise ValidationError(errors)

    # -- navigation helpers ------------------------------------------------

    @property
    def ordered_stages(self):
        """Stages in the order they are played.

        Deliberately not cached on the instance. A dashboard reads this, then acts
        on a stage, then reads it again; a cached list would keep showing the
        stages as they were before the action, which is precisely when a stale
        answer is least welcome.
        """
        return list(self.stages.select_related("advancement").order_by("order"))

    @property
    def current_stage(self):
        """The stage a school administrator would be looking at.

        The first stage that is not complete, or the last one if they all are.
        Deliberately derived rather than stored: a stored pointer would be a
        second source of truth that could disagree with the stage statuses.
        """
        stages = self.ordered_stages
        if not stages:
            return None
        for stage in stages:
            if stage.status != TournamentStageStatus.COMPLETED:
                return stage
        return stages[-1]

    @property
    def finalist_participants(self):
        """Participants still in the championship, best first.

        Everyone still live when the championship ends. Before that it is
        everyone who has not been eliminated.
        """
        live = [
            participant
            for participant in self.participants.select_related("classroom")
            if participant.status in LIVE_PARTICIPANT_STATUSES
        ]
        return sorted(live, key=lambda p: (-p.aggregate_score, p.classroom.name.lower()))

    def finalise(self, *, now=None):
        """Freeze the championship's conclusion.

        Called by :func:`tournaments.services.finalize_tournament` rather than
        directly, so that the rules about *when* a championship may be concluded
        stay in one place.
        """
        from tournaments.services import compute_final_standings

        now = now or timezone.now()
        standings = compute_final_standings(self)

        leader_row = standings[0] if standings else None

        # A championship in which nobody scored has no champion, for the same
        # reason `scoring.services.finalise` gives such a round no winner:
        # naming one would announce a result that was not earned. Ranking by
        # aggregate score alone would always produce a "winner" here, because
        # every classroom is level at zero.
        if leader_row is not None and leader_row["score"] <= 0:
            leader_row = None

        leader = (
            self.participants.select_related("classroom")
            .filter(pk=leader_row["participant_id"])
            .first()
            if leader_row
else None
        )

        finalists = self.finalist_participants
        if leader is not None:
            for participant in finalists:
                if participant.pk == leader.pk:
                    participant.status = TournamentParticipantStatus.CHAMPION
                    participant.save(update_fields=["status", "updated_at"])
                    break

        # Recomputed *after* the promotion, so the frozen table records the
        # winner's status as champion. The first pass is taken before the winner is
        # known, so freezing it as-is would store a row calling the champion
        # merely "qualified" in the same record that names it the winner. The
        # ranking is unaffected: it is ordered by aggregate score, not by status.
        standings = compute_final_standings(self)

        record, _ = TournamentRecord.objects.update_or_create(
            tournament=self,
            defaults={
                "season": self.season,
                "subject": self.subject,
                "grade": self.grade,
                "winner": leader.classroom if leader else None,
                "winner_name": leader.classroom.display_name if leader else "",
                "standings": standings,
                "participants_count": self.participants.count(),
                "stages_completed": self.stages.filter(
                    status=TournamentStageStatus.COMPLETED
                ).count(),
                "completed_at": now,
                "generated_at": now,
            },
        )
        return record


class TournamentStage(models.Model):
    """One round of a championship, played with ordinary competitions."""

    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="stages",
    )
    stage_type = models.CharField(
        max_length=32,
        choices=TournamentStageType.choices,
        help_text="Which round this is. One of each type per tournament.",
    )
    name = models.CharField(
        max_length=160,
        blank=True,
        help_text="Optional label. Left blank, the stage type's name is used.",
    )
    order = models.PositiveIntegerField(
        help_text="1-based order this stage is played in.",
    )
    status = models.CharField(
        max_length=16,
        choices=TournamentStageStatus.choices,
        default=TournamentStageStatus.PENDING,
        db_index=True,
        help_text="Driven by the attached competitions and by advancement.",
    )
    advancing_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "How many classrooms advance to the next stage. Leave blank to "
            "advance every classroom that is still in the championship, which "
            "is what a final normally does."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "tournament stage"
        verbose_name_plural = "tournament stages"
        ordering = ["tournament_id", "order"]
        constraints = [
            # Two stages at the same position would make "the next stage"
            # ambiguous, which is the one lookup the whole advancement path
            # depends on.
            models.UniqueConstraint(
                fields=["tournament", "order"],
                name="tournaments_one_stage_per_position",
            ),
            # One qualification, one semi final, one final. A championship with
            # two "Semi final" stages could not be read off the data at all.
            models.UniqueConstraint(
                fields=["tournament", "stage_type"],
                name="tournaments_one_stage_per_type",
            ),
            models.CheckConstraint(
                condition=models.Q(advancing_count__isnull=True)
                | models.Q(advancing_count__gte=1),
                name="tournaments_advancing_count_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.tournament})"

    @property
    def display_name(self) -> str:
        return self.name or self.get_stage_type_display()

    @property
    def is_completed(self) -> bool:
        return self.status == TournamentStageStatus.COMPLETED

    @property
    def is_open(self) -> bool:
        """Whether this stage still accepts edits."""
        return self.status in OPEN_STAGE_STATUSES

    @property
    def advances_all(self) -> bool:
        """Whether every remaining classroom advances.

        A stage with no ``advancing_count`` does not cut anybody, which is how a
        final produces a champion without a configured cut-off.
        """
        return self.advancing_count is None

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.order < 1:
            errors["order"] = "A stage is played from position 1 onwards."
        if self.advancing_count is not None and self.advancing_count < 1:
            errors["advancing_count"] = "At least one classroom must advance."
        if errors:
            raise ValidationError(errors)

    @property
    def next_stage(self):
        """The stage that follows this one, or ``None`` if it is the last."""
        if self.tournament_id is None:
            return None
        return (
            self.tournament.stages.filter(order__gt=self.order)
            .order_by("order")
            .first()
        )

    @property
    def competition_links(self):
        """The rows linking this stage to its competitions."""
        return self.competitions.select_related("competition").order_by("created_at")

    @property
    def attached_competitions(self):
        """The competitions played in this stage.

        The ``Competition`` objects rather than the link rows, because every
        caller wants the round itself: to show its title, check whether it is
        finished, or read its frozen result.
        """
        return Competition.objects.filter(
            pk__in=self.competitions.values_list("competition_id", flat=True)
        ).order_by("id")

    def completed_competitions(self):
        """Attached rounds that have finished and had their result frozen."""
        return self.attached_competitions.filter(
            state=CompetitionState.FINISHED, result__isnull=False
        )

    def completed_results(self):
        """The frozen :class:`scoring.models.CompetitionResult` of each such round.

        Separate from :meth:`completed_competitions` because the two are easy to
        confuse and confusing them is a bug: advancement must read the frozen
        standings, which live on the result and not on the round.
        """
        from scoring.models import CompetitionResult

        return CompetitionResult.objects.filter(
            competition__in=self.completed_competitions()
        ).select_related("competition")

    def refresh_status(self, *, save: bool = True) -> str:
        """Recompute the stage status from the competitions attached to it.

        Derived state, so it is recomputed rather than trusted: an administrator
        can finish a competition in the admin and the stage must notice without
        anyone pressing a button.

        Returns the status, and saves unless ``save=False``.
        """
        from competitions.models import CompetitionState

        total = self.competitions.count()
        finished = self.competitions.filter(
            competition__state=CompetitionState.FINISHED,
            competition__result__isnull=False,
        ).count()

        if self.status == TournamentStageStatus.COMPLETED:
            new_status = TournamentStageStatus.COMPLETED
        elif total == 0:
            new_status = TournamentStageStatus.PENDING
        elif finished == total:
            new_status = TournamentStageStatus.READY
        else:
            new_status = TournamentStageStatus.RUNNING

        if new_status != self.status:
            self.status = new_status
            if save:
                self.save(update_fields=["status", "updated_at"])
        return new_status


class StageCompetition(models.Model):
    """An existing competition placed into a stage.

    A plain many-to-many would allow the same round to be counted in two stages,
    which would silently double every score in the championship. The one-to-one to
    :class:`competitions.models.Competition` is what prevents that, at the database
    level rather than only in the service that sets it up.
    """

    stage = models.ForeignKey(
        TournamentStage,
        on_delete=models.CASCADE,
        related_name="competitions",
    )
    competition = models.OneToOneField(
        "competitions.Competition",
        on_delete=models.PROTECT,
        related_name="tournament_stage",
        help_text="The round this stage is played with. Belongs to at most one stage.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "stage competition"
        verbose_name_plural = "stage competitions"
        ordering = ["stage_id", "created_at"]

    def __str__(self) -> str:
        return f"{self.competition} in {self.stage}"


class TournamentParticipant(models.Model):
    """A classroom taking part in a tournament.

    A reference to the existing :class:`classrooms.models.Classroom`, never a
    second identity system: the same room that answers over WebSocket in a
    competition is the room listed here, and its score is the one the live
    leaderboard produced.
    """

    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="participants",
    )
    classroom = models.ForeignKey(
        "classrooms.Classroom",
        on_delete=models.CASCADE,
        related_name="tournament_entries",
    )
    status = models.CharField(
        max_length=16,
        choices=TournamentParticipantStatus.choices,
        default=TournamentParticipantStatus.ENTERED,
        db_index=True,
        help_text="Where this classroom stands. Moved by the advancement service.",
    )
    current_stage = models.ForeignKey(
        TournamentStage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_participants",
        help_text="The stage this classroom is currently in, if any.",
    )
    aggregate_score = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Points earned across the stages this classroom has played. Updated "
            "when advancement is processed, and shown as a ranking aid only - a "
            "stage result is always read from the competition's frozen result."
        ),
    )
    eliminated_at_stage = models.ForeignKey(
        TournamentStage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="eliminated_participants",
        help_text="The stage this classroom was knocked out at, if it was.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager()

    class Meta:
        verbose_name = "tournament participant"
        verbose_name_plural = "tournament participants"
        ordering = ["tournament_id", "classroom__name"]
        constraints = [
            # The rule "a classroom enters a championship once". Enforced by the
            # database so that two staff members adding the same room at the same
            # moment cannot both succeed.
            models.UniqueConstraint(
                fields=["tournament", "classroom"],
                name="tournaments_one_entry_per_classroom",
            )
        ]

    def __str__(self) -> str:
        return f"{self.classroom} in {self.tournament}"

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_PARTICIPANT_STATUSES

    @property
    def display_name(self) -> str:
        return self.classroom.display_name


class StageAdvancement(models.Model):
    """The record of one stage's advancement having been decided.

    Written only when a stage is completed, which is what makes processing it a
    matter of record rather than of calculation: the calculation can be re-run,
    but the fact that advancement was decided cannot be undone or repeated.
    """

    stage = models.OneToOneField(
        TournamentStage,
        on_delete=models.CASCADE,
        related_name="advancement",
    )
    status = models.CharField(
        max_length=16,
        choices=AdvancementStatus.choices,
        default=AdvancementStatus.PROCESSED,
        help_text=(
            "'tie' means the cut fell between classrooms that are level and an "
            "administrator must choose. Stored rather than discarded, so the "
            "dashboard can show why the stage is still open."
        ),
    )
    advancing_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="The cut-off in force when this was processed, kept as a record.",
    )
    tie_detail = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "When a tie was found: the classrooms involved and the figures they "
            "were level on. Shown to the administrator who has to resolve it."
        ),
    )
    processed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tournaments_advancements",
        help_text="Who decided this. Null only if the row predates this field.",
    )
    resolved_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tournaments_tie_resolutions",
        help_text="Who resolved a tie. Null while no tie has needed resolving.",
    )
    processed_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "stage advancement"
        verbose_name_plural = "stage advancements"
        ordering = ["-processed_at"]

    def __str__(self) -> str:
        return f"Advancement for {self.stage}"

    @property
    def is_tie(self) -> bool:
        return self.status == AdvancementStatus.TIE

    @property
    def tied_classroom_ids(self) -> list[int]:
        return list(self.tie_detail.get("classroom_ids") or [])


class StageAdvancementEntry(models.Model):
    """One classroom's outcome within a stage's advancement.

    Records what the standings were at the moment the cut was taken, so the
    dashboard can explain a result months later without re-deriving it from
    competitions that may since have been corrected.
    """

    advancement = models.ForeignKey(
        StageAdvancement,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    participant = models.ForeignKey(
        TournamentParticipant,
        on_delete=models.CASCADE,
        related_name="advancement_entries",
    )
    rank = models.PositiveIntegerField(help_text="Position in the stage's standings.")
    score = models.PositiveIntegerField(default=0)
    correct_answers = models.PositiveIntegerField(default=0)
    tied = models.BooleanField(
        default=False,
        help_text="Whether this classroom was level with another on the cut-off.",
    )
    advanced = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether it moved to the next stage.",
    )

    class Meta:
        verbose_name = "stage advancement entry"
        verbose_name_plural = "stage advancement entries"
        ordering = ["advancement_id", "rank"]
        constraints = [
            models.UniqueConstraint(
                fields=["advancement", "participant"],
                name="tournaments_one_entry_per_participant_advancement",
            )
        ]

    def __str__(self) -> str:
        return f"{self.participant} rank {self.rank}"

    @property
    def classroom_name(self) -> str:
        return self.participant.classroom.display_name


class TournamentRecord(models.Model):
    """The frozen conclusion of a completed tournament.

    A snapshot, for the same reason :class:`scoring.models.CompetitionResult` is
    one. The Hall of Fame and the history report read this row, so a later
    correction to a past competition does not silently rewrite a championship the
    school has already celebrated.
    """

    tournament = models.OneToOneField(
        Tournament,
        on_delete=models.CASCADE,
        related_name="record",
    )
    season = models.CharField(
        max_length=32,
        blank=True,
        help_text="Copied from the tournament when it concluded, so history keeps it.",
    )
    subject = models.CharField(max_length=80, blank=True)
    grade = models.CharField(max_length=64, blank=True)
    winner = models.ForeignKey(
        "classrooms.Classroom",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tournament_wins",
        help_text="The winning classroom, if the championship produced one.",
    )
    winner_name = models.CharField(
        max_length=160,
        blank=True,
        help_text="Name as it stood when the championship concluded.",
    )
    standings = models.JSONField(
        default=list,
        help_text=(
            "Final ranking rows: rank, classroom, score, stages played. Frozen "
            "with the rest of the record."
        ),
    )
    participants_count = models.PositiveIntegerField(default=0)
    stages_completed = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(
        help_text="When the championship concluded.",
    )
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "tournament record"
        verbose_name_plural = "tournament records"
        ordering = ["-completed_at"]

    def __str__(self) -> str:
        return f"{self.tournament} ({self.season or 'no season'})"

    @property
    def rows(self) -> list:
        return list(self.standings or [])

    @property
    def participant_names(self) -> list[str]:
        return [row.get("classroom", "") for row in self.rows]


__all__ = [
    "AdvancementStatus",
    "LAST_STAGE_TYPE",
    "LIVE_PARTICIPANT_STATUSES",
    "MUTABLE_TOURNAMENT_STATUSES",
    
    "OPEN_STAGE_STATUSES",
    "PARTICIPATION_OPEN_STATUSES",
    "StageAdvancement",
    "StageAdvancementEntry",
    "StageCompetition",
    "Tournament",
    "TournamentParticipant",
    "TournamentParticipantStatus",
    "TournamentQuerySet",
    "TournamentRecord",
    "TournamentStage",
    "TournamentStageStatus",
    "TournamentStageType",
    "TournamentStatus",
]
