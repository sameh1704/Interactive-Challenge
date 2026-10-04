"""Tournament business logic.

Every rule that decides anything about a championship lives here, not in a view,
a form or a template. A view reads state and calls these functions; it never
computes an outcome. That is what makes the whole championship layer testable
without a browser, and it is what guarantees the answers to the questions the
project cares about most:

* **who advances** is decided here, from frozen competition results, on the
  server. No client sends a winner, a score or a status;
* **an ambiguous cut** is refused rather than guessed - see
  :func:`calculate_advancement` and :func:`process_advancement`;
* **a completed stage** cannot be advanced twice, because the record of that
  decision is a row, not a calculation.

The tie policy
--------------
Advancement is ranked on **points, then correct answers**. Both come from the
frozen :class:`scoring.models.CompetitionResult` of each competition in the stage,
so they are exact integers and cannot disagree between two runs.

If two classrooms are level on *both*, the cut has no defensible answer: the
scoring produced the same result, so picking either one would announce a
difference the rules never made. Rather than break the tie with a metric invented
for the occasion, the system **records the tie and refuses to advance anyone**
until an administrator names the advancing classrooms
(:func:`resolve_tie`). The tie detail carries the figures involved, so whoever
resolves it is deciding on the same information the room was shown.

Average response time is deliberately *not* part of this rule. It is a tie-break in
:class:`scoring.leaderboard` because a leaderboard must be totally ordered; a
championship cut is a different decision, and using a rounded per-class average
to split two classrooms on identical points and identical correct answers would
make the cut depend on presentation rounding. Deferring to an administrator is
both deterministic and honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from competitions.models import CompetitionState
from tournaments.models import (
    AdvancementStatus,
    PARTICIPATION_OPEN_STATUSES,
    StageAdvancement,
    StageAdvancementEntry,
    StageCompetition,
    Tournament,
    TournamentParticipant,
    TournamentParticipantStatus,
    TournamentRecord,
    TournamentStage,
    TournamentStageStatus,
    TournamentStageType,
    TournamentStatus,
)

# The comparison that decides a cut. Score first, then correct answers.
#
# Expressed as a sort key so that ordering and tie detection cannot disagree:
# two rows tie exactly when their keys are equal.
def standing_key(score: int, correct_answers: int) -> tuple[int, int]:
    """The comparison a championship cut is decided on.

    Negative scores sort descending, so a larger key means a worse standing.
    """
    return (-int(score or 0), -int(correct_answers or 0))


class TournamentError(Exception):
    """A tournament action was refused. ``message`` is safe to show staff."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class StageStanding:
    """One classroom's aggregated result within a stage.

    ``rank`` is the position the classroom shares with any other it is level
    with, numbered 1, 2, 2, 4 - the same convention
    :class:`scoring.models.CompetitionResult` uses, so a shared position on a
    tournament board means the same thing as one on a live leaderboard.
    """

    participant_id: int
    classroom_id: int
    classroom_name: str
    score: int
    correct_answers: int
    competitions_played: int
    rank: int = 0

    @property
    def sort_key(self) -> tuple[int, int]:
        return standing_key(self.score, self.correct_answers)


@dataclass(frozen=True)
class AdvancementPlan:
    """What advancing a stage would do. Computed without writing anything."""

    stage_id: int
    advancing_count: int | None
    standings: list[StageStanding]
    advancing: list[StageStanding] = field(default_factory=list)
    eliminated: list[StageStanding] = field(default_factory=list)
    tie: list[StageStanding] = field(default_factory=list)
    #: The standing the cut falls on. Everything level with it is ambiguous,
    #: which is what :attr:`has_tie` reports.
    boundary: StageStanding | None = None

    @property
    def has_tie(self) -> bool:
        """Whether the cut falls between classrooms that are level."""
        return bool(self.tie)

    @property
    def competing(self) -> list[StageStanding]:
        """Classrooms eligible to be ranked in this stage."""
        return self.standings

    @property
    def standings_with_ranks(self) -> list[dict]:
        return [
            {
                "rank": standing.rank,
                "participant_id": standing.participant_id,
                "classroom_id": standing.classroom_id,
                "classroom": standing.classroom_name,
                "score": standing.score,
                "correct_answers": standing.correct_answers,
                "advances": standing in self.advancing,
                "tied": standing in self.tie,
            }
            for standing in self.standings
        ]


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------


def assert_can_manage(tournament: Tournament, user) -> None:
    """Refuse anyone who may not change this tournament.

    The same rule the live engine uses for a round - owning teacher or an
    administrator - so a member of staff who may drive a competition may also set
    up the championship it belongs to.
    """
    from accounts.permissions import is_administrator

    if not user or not user.is_authenticated or not user.is_active:
        raise TournamentError("not_signed_in", "Sign in to manage tournaments.")
    if tournament.created_by_id == user.pk or is_administrator(user):
        return
    raise TournamentError(
        "not_authorised", "You are not the teacher running this tournament."
    )


# ---------------------------------------------------------------------------
# Creating a tournament
# ---------------------------------------------------------------------------


@transaction.atomic
def create_tournament(
    *,
    name: str,
    created_by,
    description: str = "",
    subject: str = "",
    grade: str = "",
    season: str = "",
    start_date=None,
    end_date=None,
) -> Tournament:
    """Create a tournament in ``draft``.

    A draft is deliberately not playable: nothing may be attached to it, so a
    championship cannot be half-built and then run by accident.
    """
    name = (name or "").strip()
    if not name:
        raise TournamentError("name_required", "Give the tournament a name.")

    if start_date and end_date and end_date < start_date:
        raise TournamentError(
            "invalid_dates", "The end date cannot be before the start date."
        )

    return Tournament.objects.create(
        name=name,
        created_by=created_by,
        description=description or "",
        subject=subject or "",
        grade=grade or "",
        season=season or "",
        start_date=start_date,
        end_date=end_date,
        status=TournamentStatus.DRAFT,
    )


@transaction.atomic
def open_tournament(tournament: Tournament) -> Tournament:
    """Move a draft to ``open``, so stages and classrooms may be added."""
    if tournament.status == TournamentStatus.COMPLETED:
        raise TournamentError(
            "already_completed", "This tournament has already concluded."
        )
    if tournament.status == TournamentStatus.CANCELLED:
        raise TournamentError(
            "cancelled", "This tournament was cancelled and cannot be opened."
        )
    if tournament.status != TournamentStatus.DRAFT:
        raise TournamentError(
            "not_draft", "Only a draft tournament can be opened."
        )
    if not tournament.participants.exists():
        raise TournamentError(
            "no_participants", "Add at least one classroom before opening."
        )

    tournament.status = TournamentStatus.OPEN
    tournament.save(update_fields=["status", "updated_at"])
    return tournament


@transaction.atomic
def cancel_tournament(tournament: Tournament) -> Tournament:
    """Cancel a tournament that will not be played.

    Allowed while it is unopened or running, because a lesson can be called off
    mid-morning. Refused once it has concluded: a championship that was played
    and celebrated is not the same thing as one that never happened.
    """
    if tournament.is_completed:
        raise TournamentError(
            "already_completed",
            "A completed tournament is a historical record and cannot be cancelled.",
        )
    if tournament.status == TournamentStatus.CANCELLED:
        raise TournamentError("already_cancelled", "This tournament is already cancelled.")

    tournament.status = TournamentStatus.CANCELLED
    tournament.save(update_fields=["status", "updated_at"])
    return tournament


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------


@transaction.atomic
def add_participant(tournament: Tournament, classroom) -> TournamentParticipant:
    """Enter a classroom into a tournament.

    Refuses a duplicate, and refuses once the tournament has started *playing* -
    including while it is ``running``. A room added halfway through would appear
    in a later stage's standings having played no earlier stage, which is the one
    thing a championship must not contain.

    Participation is therefore something a tournament is built with, and only
    advancement is what takes a classroom further.
    """
    if tournament.status not in PARTICIPATION_OPEN_STATUSES:
        raise TournamentError(
            "tournament_locked",
            "This tournament is closed; its participants can no longer be changed.",
        )
    if classroom is None:
        raise TournamentError("classroom_required", "Choose a classroom.")

    existing = tournament.participants.filter(classroom=classroom).first()
    if existing is not None:
        raise TournamentError(
            "duplicate_participant",
            f"{classroom.display_name} is already taking part in this tournament.",
        )

    return TournamentParticipant.objects.create(
        tournament=tournament,
        classroom=classroom,
        status=TournamentParticipantStatus.ENTERED,
    )


@transaction.atomic
def remove_participant(tournament: Tournament, participant: TournamentParticipant) -> None:
    """Remove a classroom from a tournament that has not started playing."""
    if not tournament.is_mutable:
        raise TournamentError(
            "tournament_locked", "This tournament is closed."
        )
    if tournament.status == TournamentStatus.RUNNING:
        raise TournamentError(
            "tournament_running",
            "This tournament has started playing; withdraw the classroom instead of "
            "removing it, so its earlier results are kept.",
        )
    participant.delete()


@transaction.atomic
def withdraw_participant(participant: TournamentParticipant) -> TournamentParticipant:
    """Mark a classroom as withdrawn without erasing what it already scored."""
    if participant.tournament.is_completed:
        raise TournamentError(
            "tournament_locked", "This tournament is closed."
        )
    participant.status = TournamentParticipantStatus.WITHDRAWN
    participant.save(update_fields=["status", "updated_at"])
    return participant


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


@transaction.atomic
def create_stage(
    tournament: Tournament,
    stage_type: str,
    *,
    name: str = "",
    advancing_count: int | None = None,
    order: int | None = None,
) -> TournamentStage:
    """Add a stage to a tournament.

    ``order`` defaults to the end of the championship, which is what adding a
    semi final between two existing stages needs the operator to override.
    """
    if not tournament.is_mutable:
        raise TournamentError(
            "tournament_locked",
            "This tournament is closed; its stages can no longer be changed.",
        )
    if stage_type not in set(TournamentStageType.values):
        raise TournamentError(
            "unknown_stage_type", f"{stage_type!r} is not a stage type."
        )

    highest = tournament.stages.aggregate(highest=Max("order"))["highest"] or 0
    resolved_order = highest + 1 if order is None else int(order)
    if resolved_order < 1:
        raise TournamentError("invalid_order", "A stage is played from position 1 onwards.")

    if advancing_count is not None:
        advancing_count = int(advancing_count)
        if advancing_count < 1:
            raise TournamentError(
                "invalid_advancing_count",
                "At least one classroom must advance.",
            )

    stage = TournamentStage.objects.create(
        tournament=tournament,
        stage_type=stage_type,
        name=(name or "").strip(),
        order=resolved_order,
        advancing_count=advancing_count,
    )

    # A qualified classroom belongs to the next stage, but that stage may not have
    # existed when it advanced - so the stage takes its participants on creation.
    # Without this, `current_stage` would stay null for the rest of the
    # championship after a qualification and the dashboard could not say who is
    # in the round being set up.
    placed = tournament.participants.filter(
        status=TournamentParticipantStatus.QUALIFIED
    ).filter(
        Q(current_stage__isnull=True)
        | Q(current_stage__status=TournamentStageStatus.COMPLETED)
    )
    for participant in placed.select_related("classroom"):
        participant.current_stage = stage
        participant.save(update_fields=["current_stage", "updated_at"])

    # Building stages is what starts a championship playing, so an open
    # tournament becomes a running one as soon as it has any.
    if tournament.status == TournamentStatus.OPEN:
        tournament.status = TournamentStatus.RUNNING
        tournament.save(update_fields=["status", "updated_at"])

    return stage


@transaction.atomic
def attach_competition(
    stage: TournamentStage, competition
) -> StageCompetition:
    """Place an existing competition into a stage.

    The competition is the one the live engine already knows how to run: this
    only records that its result counts towards the stage. The classrooms taking
    part must already be tournament participants, otherwise a stage could be won
    by a room that never entered the championship.
    """
    if not stage.is_open:
        raise TournamentError(
            "stage_locked", "This stage is closed; its competitions cannot be changed."
        )
    if stage.tournament.status == TournamentStatus.CANCELLED:
        raise TournamentError(
            "tournament_cancelled", "This tournament was cancelled."
        )
    if competition is None:
        raise TournamentError("competition_required", "Choose a competition.")

    existing = StageCompetition.objects.filter(competition=competition).first()
    if existing is not None:
        raise TournamentError(
            "competition_in_use",
            "That competition already belongs to a tournament stage.",
        )

    participant_ids = set(
        stage.tournament.participants.values_list("classroom_id", flat=True)
    )
    competing_ids = set(competition.classrooms.values_list("classroom_id", flat=True))
    missing = competing_ids - participant_ids
    if missing:
        raise TournamentError(
            "classroom_not_a_participant",
            "Every classroom in that competition must be taking part in this "
            "tournament first.",
        )

    link = StageCompetition.objects.create(stage=stage, competition=competition)
    stage.refresh_status()
    return link


@transaction.atomic
def detach_competition(stage: TournamentStage, competition) -> None:
    """Remove a competition from a stage.

    Only while the stage has not completed: once advancement has been recorded
    the competition's result is part of the championship's history.
    """
    if stage.is_completed:
        raise TournamentError(
            "stage_locked", "This stage is complete; its competitions are part of its record."
        )
    StageCompetition.objects.filter(stage=stage, competition=competition).delete()
    stage.refresh_status()


@transaction.atomic
def refresh_stage(stage: TournamentStage) -> TournamentStage:
    """Recompute a stage's status from its competitions, then persist it."""
    stage.refresh_status()
    return stage


@transaction.atomic
def complete_stage(stage: TournamentStage) -> TournamentStage:
    """Mark a stage complete so its advancement may be processed.

    Refuses while a competition is still running: a cut taken from a leaderboard
    that is still moving is not a result.
    """
    if stage.is_completed:
        raise TournamentError("already_completed", "This stage is already complete.")

    total = stage.competitions.count()
    if total == 0:
        raise TournamentError(
            "no_competitions", "Attach a competition to this stage before completing it."
        )

    unfinished = stage.competitions.exclude(
        competition__state=CompetitionState.FINISHED,
        competition__result__isnull=False,
    ).count()
    if unfinished:
        raise TournamentError(
            "competition_running",
            "Every competition in this stage must be finished first.",
        )

    stage.status = TournamentStageStatus.COMPLETED
    stage.save(update_fields=["status", "updated_at"])

    if stage.tournament.status in {
        TournamentStatus.OPEN,
        TournamentStatus.DRAFT,
    }:
        stage.tournament.status = TournamentStatus.RUNNING
        stage.tournament.save(update_fields=["status", "updated_at"])

    return stage


# ---------------------------------------------------------------------------
# Standings and advancement
# ---------------------------------------------------------------------------


def stage_standings(stage: TournamentStage) -> list[StageStanding]:
    """Aggregate the stage's finished competitions into one ranking.

    Read from the frozen ``CompetitionResult`` of each attached competition
    rather than from live answers: a championship is decided on the results the
    rooms were shown, and re-deriving from answers would let a later correction
    change a cut that has already been announced.

    Classrooms that took part in the stage but have no result row yet are kept at
    zero, so the board shows a room that scored nothing rather than hiding it.
    """
    results = list(stage.completed_results())

    totals: dict[int, dict] = {}
    for result in results:
        for row in result.rows:
            classroom_id = row.get("classroom_id")
            if classroom_id is None:
                continue
            entry = totals.setdefault(
                classroom_id,
                {
                    "score": 0,
                    "correct_answers": 0,
                    "played": 0,
                    "name": row.get("classroom", ""),
                },
            )
            entry["score"] += int(row.get("score") or 0)
            entry["correct_answers"] += int(row.get("correct_answers") or 0)
            entry["played"] += 1

    participants = {
        participant.classroom_id: participant
        for participant in stage.tournament.participants.select_related("classroom")
    }

    # The rooms that competed decide the ranking. A participant who never entered
    # this stage has no standing in it, and must not be carried into a cut.
    #
    # Read straight from the rows rather than from `totals`, so a classroom with
    # no figures still counts as having competed.
    competing_ids = {
        row["classroom_id"]
        for result in results
        for row in result.rows
        if row.get("classroom_id") is not None
    }

    standings: list[StageStanding] = []
    for classroom_id in competing_ids:
        participant = participants.get(classroom_id)
        if participant is None:
            # The competition included a room that is not in the tournament. The
            # service refuses that when attaching, so reaching here means the
            # competition was edited afterwards; such a room cannot advance.
            continue
        if not participant.is_live:
            continue
        figures = totals.get(
            classroom_id,
            {"score": 0, "correct_answers": 0, "played": 0, "name": ""},
        )
        standings.append(
            StageStanding(
                participant_id=participant.pk,
                classroom_id=classroom_id,
                classroom_name=participant.classroom.display_name,
                score=int(figures["score"]),
                correct_answers=int(figures["correct_answers"]),
                competitions_played=int(figures["played"]),
            )
        )

    standings.sort(
        key=lambda s: s.sort_key + (s.classroom_name.lower(),),
    )
    return _with_ranks(standings)


def _with_ranks(standings: list[StageStanding]) -> list[StageStanding]:
    """Number the standings 1, 2, 2, 4 so a shared position is visible."""
    ranked: list[StageStanding] = []
    previous = None
    current_rank = 0

    for index, standing in enumerate(standings, start=1):
        key = standing.sort_key
        if previous is None or key != previous:
            current_rank = index
            previous = key
        ranked.append(
            StageStanding(
                participant_id=standing.participant_id,
                classroom_id=standing.classroom_id,
                classroom_name=standing.classroom_name,
                score=standing.score,
                correct_answers=standing.correct_answers,
                competitions_played=standing.competitions_played,
                rank=current_rank,
            )
        )
    return ranked


def calculate_advancement(stage: TournamentStage) -> AdvancementPlan:
    """Work out who would advance from a stage, writing nothing.

    Separated from :func:`process_advancement` so that the calculation can be
    tested - and shown on the dashboard as "who would advance" - without any
    possibility of a half-applied cut.
    """
    if not stage.is_completed:
        raise TournamentError(
            "stage_not_completed", "Complete this stage before processing advancement."
        )
    if hasattr(stage, "advancement"):
        raise TournamentError(
            "already_processed", "This stage's advancement has already been decided."
        )

    standings = stage_standings(stage)
    count = stage.advancing_count

    if count is None:
        return AdvancementPlan(
            stage_id=stage.pk,
            advancing_count=None,
            standings=standings,
            advancing=list(standings),
            eliminated=[],
        )

    if len(standings) < count:
        raise TournamentError(
            "insufficient_participants",
            f"This stage has {len(standings)} participating "
            f"classroom{'' if len(standings) == 1 else 's'} but {count} must advance.",
        )

    # The cut. Everything strictly ahead of the boundary advances; the boundary
    # itself is checked for a tie rather than resolved by position in the list.
    plan = cut_plan(stage)
    boundary = plan.boundary
    if boundary is None:
        raise TournamentError("stage_not_completed", "This stage has no standings.")

    below = next(
        (s for s in plan.standings if s.sort_key == boundary.sort_key and s is not boundary),
        None,
    )
    if below is not None:
        tied = [s for s in plan.standings if s.sort_key == boundary.sort_key]
        return AdvancementPlan(
            stage_id=stage.pk,
            advancing_count=count,
            standings=plan.standings,
            advancing=[],
            eliminated=[],
            tie=tied,
            boundary=boundary,
        )

    return AdvancementPlan(
        stage_id=stage.pk,
        advancing_count=count,
        standings=plan.standings,
        advancing=list(plan.advancing),
        eliminated=list(plan.eliminated),
        boundary=boundary,
    )


@transaction.atomic
def process_advancement(stage: TournamentStage, *, user=None) -> StageAdvancement:
    """Apply a stage's cut and move the winners on.

    Refuses when the stage is not complete, when it has already been processed,
    and - the important one - when the cut is ambiguous. A refused cut leaves the
    championship exactly as it was.
    """
    plan = calculate_advancement(stage)

    if plan.has_tie:
        # Recorded, not raised and forgotten: the dashboard shows why the stage
        # is still waiting on a person.
        advancement, _ = StageAdvancement.objects.update_or_create(
            stage=stage,
            defaults={
                "status": AdvancementStatus.TIE,
                "advancing_count": stage.advancing_count,
                "tie_detail": {
                    "classroom_ids": [s.classroom_id for s in plan.tie],
                    "classrooms": [s.classroom_name for s in plan.tie],
                    "score": plan.tie[0].score,
                    "correct_answers": plan.tie[0].correct_answers,
                    "advancing_count": stage.advancing_count,
                },
                "processed_by": user if _is_staff(user) else None,
                "resolved_by": None,
                "resolved_at": None,
            },
        )
        return advancement

    return _apply_advancement(stage, plan, user=user)


@transaction.atomic
def resolve_tie(
    stage: TournamentStage, classroom_ids, *, user=None
) -> StageAdvancement:
    """Apply a cut an administrator has decided by hand.

    The only way past an ambiguous cut, and it is deliberately not automatic.
    Validated against the plan so that an administrator cannot use it to advance a
    classroom that had already been eliminated, or to advance the wrong number.
    """
    stage.refresh_from_db()
    if not hasattr(stage, "advancement"):
        raise TournamentError("no_tie", "This stage has no tie to resolve.")
    if not stage.advancement.is_tie:
        raise TournamentError("not_a_tie", "This stage's advancement is not a tie.")
    if not _is_staff(user):
        raise TournamentError(
            "not_authorised", "An administrator must resolve a tie."
        )
    from accounts.permissions import is_administrator

    if not is_administrator(user):
        raise TournamentError(
            "not_authorised",
            "Only an administrator may resolve a tie between classrooms.",
        )

    chosen = sorted({int(pk) for pk in (classroom_ids or []) if pk})
    count = stage.advancing_count
    if count is None:
        raise TournamentError("not_a_tie", "This stage advances everyone; there is no tie.")

    if len(chosen) != count:
        raise TournamentError(
            "wrong_number",
            f"Choose exactly {count} classroom{'' if count == 1 else 'es'} to advance.",
        )

    plan = cut_plan(stage)
    by_id = {standing.classroom_id: standing for standing in plan.standings}
    boundary = plan.boundary
    if boundary is None:
        raise TournamentError("not_a_tie", "This stage has nothing to decide.")

    unknown = [pk for pk in chosen if pk not in by_id]
    if unknown:
        raise TournamentError(
            "not_in_stage", "One of those classrooms did not take part in this stage."
        )

    # The rule an administrator is resolving a tie under, stated once:
    #
    #   every classroom strictly ahead of the boundary must advance, and every
    #   remaining place must go to a classroom level with the boundary.
    #
    # Anything else - dropping a higher-scoring room, or promoting one that was
    # already out - would not be resolving a tie, it would be deciding the
    # result, which is exactly what this function exists to prevent.
    tied_ids = set(stage.advancement.tied_classroom_ids)
    ahead = [
        standing.classroom_id
        for standing in plan.standings
        if standing.sort_key < boundary.sort_key
    ]

    missing_ahead = [pk for pk in ahead if pk not in chosen]
    if missing_ahead:
        names = ", ".join(by_id[pk].classroom_name for pk in missing_ahead)
        raise TournamentError(
            "higher_classroom_excluded",
            f"{names} scored higher and must advance; only the tied places are "
            "yours to decide.",
        )

    # Anything level with the boundary is a tied candidate; anything strictly
    # better was already required above. So the only classroom that must be
    # refused is one that lost its place outright.
    not_tied = [
        pk for pk in chosen if by_id[pk].sort_key > boundary.sort_key
    ]
    if not_tied:
        names = ", ".join(by_id[pk].classroom_name for pk in not_tied)
        raise TournamentError(
            "not_a_tie_classroom",
            f"{names} finished below the cut-off and cannot be advanced.",
        )

    selected = {by_id[pk] for pk in chosen}
    advancing = sorted(
        selected, key=lambda s: s.sort_key + (s.classroom_name.lower(),)
    )
    resolved_plan = AdvancementPlan(
        stage_id=stage.pk,
        advancing_count=count,
        standings=plan.standings,
        advancing=advancing,
        eliminated=[s for s in plan.standings if s not in selected],
        boundary=boundary,
    )

    return _apply_advancement(stage, resolved_plan, user=user, resolved=True)


def cut_plan(stage: TournamentStage) -> AdvancementPlan:
    """The standings of a completed stage, with the cut-off marked.

    The plan before tie detection, so it is always computable even when the cut
    is ambiguous. :func:`calculate_advancement` wraps this and reports the tie;
    :func:`resolve_tie` uses it to check an administrator's choice against the
    same numbers.
    """
    standings = stage_standings(stage)
    count = stage.advancing_count

    if count is None:
        return AdvancementPlan(
            stage_id=stage.pk,
            advancing_count=None,
            standings=standings,
            advancing=list(standings),
            boundary=standings[-1] if standings else None,
        )

    boundary = standings[count - 1] if len(standings) >= count and standings else None
    chosen = standings[:count] if boundary is not None else []

    return AdvancementPlan(
        stage_id=stage.pk,
        advancing_count=count,
        standings=standings,
        advancing=list(chosen),
        eliminated=list(standings[len(chosen) :]),
        boundary=boundary,
    )


def _apply_advancement(
    stage: TournamentStage, plan: AdvancementPlan, *, user=None, resolved: bool = False
) -> StageAdvancement:
    """Write a decided cut: the record, the entries and the participants."""
    now = timezone.now()
    advancing_ids = {standing.classroom_id for standing in plan.advancing}
    tied_ids = {standing.classroom_id for standing in plan.tie}

    advancement, _ = StageAdvancement.objects.update_or_create(
        stage=stage,
        defaults={
            "status": AdvancementStatus.PROCESSED,
            "advancing_count": stage.advancing_count,
            "tie_detail": {},
            "processed_by": user if _is_staff(user) else None,
            "resolved_by": user if (resolved and _is_staff(user)) else None,
            "processed_at": now,
            "resolved_at": now if resolved else None,
        },
    )

    entries = []
    for standing in plan.standings:
        entry, _ = StageAdvancementEntry.objects.update_or_create(
            advancement=advancement,
            participant_id=standing.participant_id,
            defaults={
                "rank": standing.rank,
                "score": standing.score,
                "correct_answers": standing.correct_answers,
                "tied": standing.classroom_id in tied_ids,
                "advanced": standing.classroom_id in advancing_ids,
            },
        )
        entries.append(entry)

    next_stage = stage.next_stage

    for participant in stage.tournament.participants.select_related("classroom"):
        standing = next(
            (s for s in plan.standings if s.participant_id == participant.pk), None
        )
        if standing is None:
            # Not ranked in this stage: leave its status alone.
            continue

        updates = [
            "aggregate_score",
            "status",
            "current_stage",
            "eliminated_at_stage",
            "updated_at",
        ]
        participant.aggregate_score = standing.score

        if participant.classroom_id in advancing_ids:
            participant.status = TournamentParticipantStatus.QUALIFIED
            participant.current_stage = next_stage
            participant.eliminated_at_stage = None
        else:
            participant.status = TournamentParticipantStatus.ELIMINATED
            participant.eliminated_at_stage = stage
            participant.current_stage = stage

        participant.save(update_fields=updates)

    if next_stage is not None and next_stage.status == TournamentStageStatus.PENDING:
        next_stage.refresh_status()

    return advancement


def _is_staff(user) -> bool:
    from accounts.permissions import is_staff_member

    return is_staff_member(user)


# ---------------------------------------------------------------------------
# Concluding a tournament
# ---------------------------------------------------------------------------


@transaction.atomic
def finalize_tournament(tournament: Tournament, *, now=None) -> TournamentRecord:
    """Conclude a tournament and freeze its record.

    Requires at least one completed stage, and refuses a tournament that is
    cancelled. The winner is derived here, on the server, from the aggregate
    scores the advancement process recorded.
    """
    if tournament.is_completed:
        raise TournamentError(
            "already_completed", "This tournament has already been finalized."
        )
    if tournament.is_cancelled:
        raise TournamentError(
            "cancelled", "A cancelled tournament has no final result."
        )

    if not tournament.stages.filter(status=TournamentStageStatus.COMPLETED).exists():
        raise TournamentError(
            "no_completed_stage",
            "Complete at least one stage before finalizing this tournament.",
        )

    now = now or timezone.now()
    record = tournament.finalise(now=now)

    tournament.status = TournamentStatus.COMPLETED
    tournament.end_date = tournament.end_date or now.date()
    tournament.save(update_fields=["status", "end_date", "updated_at"])

    return record


def compute_final_standings(tournament: Tournament) -> list[dict]:
    """The championship's final ranking, as plain rows for the frozen record."""
    participants = list(
        tournament.participants.select_related("classroom").order_by("classroom__name")
    )
    live = [
        participant
        for participant in participants
        if participant.status
        in {
            TournamentParticipantStatus.QUALIFIED,
            TournamentParticipantStatus.CHAMPION,
        }
    ]

    ordered = sorted(
        live,
        key=lambda p: (
            standing_key(p.aggregate_score, 0),
            p.classroom.name.lower(),
        ),
    )

    rows = []
    previous_key = None
    current_rank = 0
    for index, participant in enumerate(ordered, start=1):
        key = standing_key(participant.aggregate_score, 0)
        if previous_key is None or key != previous_key:
            current_rank = index
            previous_key = key
        rows.append(
            {
                "rank": current_rank,
                "participant_id": participant.pk,
                "classroom_id": participant.classroom_id,
                "classroom": participant.classroom.display_name,
                "score": participant.aggregate_score,
                "status": participant.status,
                "stages": participant.advancement_entries.filter(
                    advanced=True
                ).count(),
            }
        )
    return rows


def history_for(tournament: Tournament) -> dict:
    """Everything needed to describe a concluded tournament.

    Assembled from relationships rather than copied, so it cannot drift from the
    competitions it describes. Used by the history view and the tournament report.
    """
    record = getattr(tournament, "record", None)
    stages = list(
        tournament.stages.prefetch_related("competitions__competition").order_by("order")
    )
    participants = list(
        tournament.participants.select_related("classroom").order_by("classroom__name")
    )

    return {
        "tournament": tournament,
        "record": record,
        "season": record.season if record else tournament.season,
        "stages": [
            {
                "stage": stage,
                "competitions": [link.competition for link in stage.competitions.all()],
                "result": (
                    stage.completed_results().first() if stage.is_completed else None
                ),
            }
            for stage in stages
        ],
        "participants": participants,
        "standings": record.rows if record else [],
    }


def hall_of_fame(limit: int | None = None) -> list[TournamentRecord]:
    """Completed tournaments, most recent first.

    Read from the frozen records only, so this is history rather than a live
    computation: a tournament that has not been finalized does not appear, however
    its competitions went.
    """
    records = TournamentRecord.objects.select_related("tournament", "winner")
    if limit:
        records = records[: int(limit)]
    return list(records)


__all__ = [
    "AdvancementPlan",
    "StageStanding",
    "TournamentError",
    "add_participant",
    "assert_can_manage",
    "attach_competition",
    "calculate_advancement",
    "cancel_tournament",
    "complete_stage",
    "compute_final_standings",
    "create_stage",
    "create_tournament",
    "cut_plan",
    "detach_competition",
    "finalize_tournament",
    "hall_of_fame",
    "history_for",
    "open_tournament",
    "process_advancement",
    "refresh_stage",
    "remove_participant",
    "resolve_tie",
    "stage_standings",
    "standing_key",
    "withdraw_participant",
]
