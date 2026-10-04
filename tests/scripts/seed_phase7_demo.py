"""Seed realistic data for the Phase 7 manual walkthrough.

Creates the school-level fixtures an administrator would already have before
running a championship: four classrooms, two *finished* competitions with frozen
results, and an administrator account to sign in as.

Run from the host with::

    Get-Content -Raw tests/scripts/seed_phase7_demo.py |
        docker compose exec -T challenge-web python manage.py shell

Idempotent and repeatable: running it again leaves the same school-level data and
clears only the championships the walkthrough itself created, so the walkthrough
can be run any number of times. The password is read from ``SEED_PASSWORD`` and is
only ever a development convenience - it is never written to the repository.
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.roles import Role
from competitions.models import (
    Competition,
    CompetitionClassroom,
    CompetitionQuestion,
    CompetitionState,
)
from classrooms.models import Classroom
from questions.models import Question
from scoring import services as scoring_services
from tournaments.models import Tournament

UserModel = get_user_model()

PASSWORD = os.environ.get("SEED_PASSWORD", "manual-test-password-not-real")

# The name the walkthrough gives the championship it builds.
WALKTHROUGH_TOURNAMENT = "Manual Primary Championship"

LABS = (
    "Science Lab A",
    "Science Lab B",
    "Science Lab C",
    "Science Lab D",
)

# How many of the round's questions each classroom answers correctly. Scores are
# computed from these, never typed in.
CORRECT = {
    "Science Lab A": 3,
    "Science Lab B": 2,
    "Science Lab C": 1,
    "Science Lab D": 1,
}


def ensure_administrator() -> None:
    user, created = UserModel.objects.get_or_create(
        username="champ.admin",
        defaults={"role": Role.ADMINISTRATOR, "full_name": "Championship Admin"},
    )
    if created:
        user.set_password(PASSWORD)
        user.save()
        print("created administrator champion.admin")
    else:
        print("administrator champion.admin already exists")


def ensure_classrooms() -> list[Classroom]:
    classrooms = []
    for name in LABS:
        classroom, _ = Classroom.objects.get_or_create(
            name=name, defaults={"grade": "Grade 5", "building": "Main"}
        )
        classrooms.append(classroom)
    print(f"{len(classrooms)} classrooms ready")
    return classrooms


def make_question(index: int) -> Question:
    question, _ = Question.objects.get_or_create(
        text=f"Demo science question {index}",
        defaults={
            "options": ["Alpha", "Beta", "Gamma", "Delta"],
            "correct_option": "Beta",
            "duration_seconds": 30,
        },
    )
    return question


def ensure_finished_competition(
    title: str, classrooms: list[Classroom], correct: dict[str, int]
) -> Competition:
    """A finished round with a frozen result, built through the scoring services."""
    existing = Competition.objects.filter(title=title).first()
    if existing is not None:
        print(f"competition {title!r} already exists (finished={existing.is_finished})")
        return existing

    competition = Competition.objects.create(
        title=title,
        teacher=UserModel.objects.filter(role=Role.TEACHER).first()
        or UserModel.objects.first(),
    )
    for classroom in classrooms:
        CompetitionClassroom.objects.create(
            competition=competition, classroom=classroom
        )

    questions = [make_question(index) for index in range(1, 4)]
    for position, question in enumerate(questions, start=1):
        CompetitionQuestion.objects.create(
            competition=competition, question=question, position=position
        )

    # Played through the live engine and the real scoring service, so the frozen
    # result is produced by exactly the code a live round uses - including the
    # speed bonus, which is why the totals are not round multiples of 100.
    from live import services as live_services

    live_services.start_competition(competition)
    for position in range(1, len(questions) + 1):
        live_services.start_question(competition, position=position)
        question = competition.current_question
        for classroom in classrooms:
            wanted = correct.get(classroom.name, 0)
            selection = question.answer_key if position <= wanted else _wrong_option(question)
            scoring_services.submit_answer(
                competition=competition,
                classroom=classroom,
                selection=selection,
            )
        live_services.close_answering(competition, reason="demo round complete")

    live_services.finish_competition(competition)
    competition.refresh_from_db()
    scoring_services.finalise(competition)
    print(f"created finished competition {title!r}")
    return competition


def _wrong_option(question: Question) -> str:
    """A legal answer that is not the key, so a wrong answer is actually recorded."""
    for option in reversed(question.options):
        if option != question.answer_key:
            return option
    return "definitely not the answer"


def reset_walkthrough_tournaments() -> None:
    """Delete the championships the walkthrough created on an earlier run.

    A round belongs to at most one stage, for good reason: allowing it in two
    stages would silently double every score in the championship. That also means
    a finished walkthrough leaves the demo rounds attached to it, so without this
    the next run could not attach them again. Only tournaments carrying the
    walkthrough's own name are removed - the classrooms and the finished rounds,
    which are the fixtures, are left alone.
    """
    stale = Tournament.objects.filter(name=WALKTHROUGH_TOURNAMENT)
    if not stale.exists():
        return
    count = stale.count()
    stale.delete()  # cascades to stages, stage links, participants and records
    print(f"removed {count} tournament(s) from an earlier walkthrough run")


def main() -> None:
    ensure_administrator()
    reset_walkthrough_tournaments()
    classrooms = ensure_classrooms()
    ensure_finished_competition("Demo qualification round", classrooms, CORRECT)

    # A separate final with a clear winner, for the second stage.
    final_scores = dict(CORRECT)
    final_scores["Science Lab A"] = 3
    final_scores["Science Lab B"] = 1
    ensure_finished_competition("Demo final round", classrooms[:2], final_scores)
    print("seed complete")


# Run on import rather than under a `__main__` guard, so the file works both when
# piped into `manage.py shell` and when run by the walkthrough script.
main()
