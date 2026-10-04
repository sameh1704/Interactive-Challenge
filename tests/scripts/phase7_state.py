"""Print a JSON snapshot of one tournament's persisted state.

The walkthrough drives the real pages over HTTP, but an HTML page can only prove
what it *says*. Confirming that an action actually persisted - that the
qualification round really was attached to that stage, that two classrooms really
were marked qualified - needs to read the stored state.

This is a read-only reporting script. It computes nothing and writes nothing; it
only serialises what is already in the database, so the walkthrough can assert
against it. Verifying through it does not weaken the end-to-end path: the
*actions* are still performed over HTTP by a real session with real CSRF tokens,
and this only checks what those actions left behind.

It reads the most recently created tournament, which is the one the walkthrough
just ran, and reports it on a single ``STATE_JSON:`` line for the caller to parse.

Run from the host with::

    Get-Content tests/scripts/phase7_state.py -Raw |
        docker compose exec -T challenge-web python manage.py shell
"""

from __future__ import annotations

import json

from classrooms.models import Classroom
from competitions.models import Competition
from tournaments.models import Tournament, TournamentParticipantStatus

# The round the seed plays for the qualification stage. Its frozen standings name
# exactly the classrooms that took part, which is how this script identifies them
# without hard-coding names or assuming a clean database.
QUALIFIER_TITLE = "Demo qualification round"


def seeded_classrooms() -> list[dict]:
    """The classrooms in the seeded qualification round, as the UI labels them.

    Read from the round's frozen standings, so the answer comes from the same data
    the championship ranks on. ``label`` is what the classroom select actually
    shows, which is not always the bare classroom name.
    """
    competition = Competition.objects.filter(title=QUALIFIER_TITLE).first()
    if competition is None or competition.result is None:
        return []
    classrooms = {
        classroom.pk: classroom
        for classroom in Classroom.objects.filter(
            pk__in=[
                row["classroom_id"]
                for row in competition.result.standings
                if row.get("classroom_id") is not None
            ]
        )
    }
    return [
        {
            "name": row.get("classroom", ""),
            "id": row.get("classroom_id"),
            "label": classrooms[row["classroom_id"]].display_name,
        }
        for row in competition.result.standings
        if row.get("classroom_id") in classrooms
    ]


def _stage_snapshot(stage) -> dict:
    return {
        "pk": stage.pk,
        "stage_type": stage.stage_type,
        "name": stage.display_name,
        "status": stage.status,
        "advancing_count": stage.advancing_count,
        "competitions": [
            competition.title for competition in stage.attached_competitions.all()
        ],
    }


def _advancement_snapshot(advancement) -> dict | None:
    if advancement is None:
        return None
    advanced, eliminated = [], []
    for entry in advancement.entries.select_related("participant__classroom"):
        name = entry.participant.classroom.name
        if entry.advanced:
            advanced.append(name)
        else:
            eliminated.append(name)
    return {
        "status": advancement.status,
        "advancing_count": advancement.advancing_count,
        "advanced": sorted(advanced),
        "eliminated": sorted(eliminated),
    }


def snapshot() -> dict:
    seeded = seeded_classrooms()
    tournament = Tournament.objects.order_by("-pk").first()
    if tournament is None:
        return {"tournament": None, "seeded_classrooms": seeded}

    stages = {
        stage.stage_type: stage for stage in tournament.stages.order_by("order")
    }
    qualification = stages.get("qualification")
    qualification_advancement = (
        getattr(qualification, "advancement", None) if qualification else None
    )

    participants = [
        {
            "classroom": participant.classroom.name,
            "classroom_id": participant.classroom_id,
            "status": participant.status,
            "aggregate_score": participant.aggregate_score,
        }
        for participant in tournament.participants.select_related("classroom").order_by(
            "classroom__name"
        )
    ]

    # The qualification round must hang off the qualification stage of this
    # tournament, and off nothing else anywhere in the database - not merely off
    # the right stage here while also being counted somewhere else.
    attachments = list(
        Competition.objects.filter(title=QUALIFIER_TITLE)
        .values_list(
            "tournament_stage__stage__stage_type",
            "tournament_stage__stage__tournament_id",
        )
    )

    record = getattr(tournament, "record", None)

    return {
        "seeded_classrooms": seeded,
        "tournament": {
            "id": tournament.pk,
            "name": tournament.name,
            "season": tournament.season,
            "status": tournament.status,
        },
        "participants": participants,
        "qualified": sorted(
            participant["classroom"]
            for participant in participants
            if participant["status"]
            in {
                TournamentParticipantStatus.QUALIFIED,
                TournamentParticipantStatus.CHAMPION,
            }
        ),
        "eliminated": sorted(
            participant["classroom"]
            for participant in participants
            if participant["status"] == TournamentParticipantStatus.ELIMINATED
        ),
        "qualification_stage": (
            _stage_snapshot(qualification) if qualification else None
        ),
        "qualification_advancement": _advancement_snapshot(qualification_advancement),
        "qualifier_attachments": [
            {"stage_type": stage_type, "tournament_id": tournament_id}
            for stage_type, tournament_id in attachments
            if stage_type is not None
        ],
        "wrong_stage_attached": any(
            stage_type != "qualification" or tournament_id != tournament.pk
            for stage_type, tournament_id in attachments
            if stage_type is not None
        ),
        "final_stage": _stage_snapshot(stages["final"]) if "final" in stages else None,
        "record": (
            {
                "winner_name": record.winner_name,
                "season": record.season,
                "participants_count": record.participants_count,
                "stages_completed": record.stages_completed,
                "standings": record.rows,
            }
            if record is not None
            else None
        ),
    }


# A marked, single-line payload, so the caller can pick it out of the
# interpreter's banner and any log output.
print("STATE_JSON:" + json.dumps(snapshot(), default=str))