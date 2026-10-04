"""Reports.

Derived entirely from stored data: ``scoring.Answer`` rows for anything about
answers, and ``scoring.CompetitionResult`` snapshots for anything about a
finished round. No report keeps figures of its own.

That single rule is what keeps a report honest. A competition report and the live
leaderboard cannot disagree, because neither of them is where the number came
from - both read the same stored answers. The alternative, each report computing
its own totals, is how two pages end up printing different scores for the same
round.

Report functions return plain dictionaries rather than rendering, so the same
function serves the HTML page and the CSV export, and can be tested without
either.
"""

from __future__ import annotations

from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from competitions.models import Competition, CompetitionState
from scoring.models import Answer


def _accuracy(correct: int, given: int) -> float | None:
    """Correct as a percentage of answers given, or ``None`` if nothing was given.

    ``None`` rather than 0.0: "no answers" and "every answer wrong" are different
    facts, and a report that showed 0% for a round nobody played in would be
    reporting a result that did not happen.
    """
    if not given:
        return None
    return round(100.0 * correct / given, 1)


def competition_report(competition: Competition) -> dict:
    """Everything worth reporting about one competition.

    Reads the stored answers, grouped once per classroom and once per question, so
    the figures agree with the leaderboard and with the reveal payload the rooms
    were shown.
    """
    answers = list(
        Answer.objects.for_competition(competition)
        .select_related("classroom", "question")
        .order_by("position", "classroom__name")
    )

    per_classroom: dict[int, dict] = {}
    per_question: dict[int, dict] = {}

    for answer in answers:
        classroom = per_classroom.setdefault(
            answer.classroom_id,
            {
                "classroom_id": answer.classroom_id,
                "classroom": answer.classroom.display_name,
                "correct": 0,
                "wrong": 0,
                "score": 0,
                "response_total": 0.0,
            },
        )
        classroom["score"] += answer.total_score
        classroom["response_total"] += answer.response_time_seconds
        if answer.is_correct:
            classroom["correct"] += 1
        else:
            classroom["wrong"] += 1

        question = per_question.setdefault(
            answer.position,
            {
                "position": answer.position,
                "question": answer.question.text,
                "question_type": answer.question.get_question_type_display(),
                "correct_answer": answer.correct_answer,
                "given": 0,
                "correct": 0,
                "response_total": 0.0,
            },
        )
        question["given"] += 1
        question["response_total"] += answer.response_time_seconds
        if answer.is_correct:
            question["correct"] += 1

    # Every classroom that was *included* appears, even one that never answered:
    # a school asking "did Lab 3 take part?" needs the answer to be "no", not to
    # find the classroom missing from the report.
    included = {
        entry.classroom_id: entry.classroom.display_name
        for entry in competition.classrooms.select_related("classroom")
    }
    for classroom_id, name in included.items():
        per_classroom.setdefault(
            classroom_id,
            {
                "classroom_id": classroom_id,
                "classroom": name,
                "correct": 0,
                "wrong": 0,
                "score": 0,
                "response_total": 0.0,
            },
        )

    classroom_rows = []
    for row in per_classroom.values():
        given = row["correct"] + row["wrong"]
        classroom_rows.append(
            {
                **row,
                "given": given,
                "accuracy": _accuracy(row["correct"], given),
                "average_response_seconds": (
                    round(row["response_total"] / given, 2) if given else None
                ),
            }
        )
    classroom_rows.sort(key=lambda r: (-r["score"], r["classroom"].lower()))

    question_rows = []
    for row in per_question.values():
        question_rows.append(
            {
                **row,
                "wrong": row["given"] - row["correct"],
                "accuracy": _accuracy(row["correct"], row["given"]),
                "average_response_seconds": (
                    round(row["response_total"] / row["given"], 2)
                    if row["given"]
                    else None
                ),
            }
        )
    question_rows.sort(key=lambda r: r["position"])

    return {
        "competition": competition,
        "date": competition.finished_at or competition.started_at,
        "state": competition.state,
        "state_display": competition.get_state_display(),
        "total_questions": competition.total_questions,
        "classrooms_count": competition.classrooms.count(),
        "answers_count": len(answers),
        "correct_count": sum(1 for a in answers if a.is_correct),
        "wrong_count": sum(1 for a in answers if not a.is_correct),
        "accuracy": _accuracy(
            sum(1 for a in answers if a.is_correct), len(answers)
        ),
        "classrooms": classroom_rows,
        "questions": question_rows,
        "result": getattr(competition, "result", None),
    }


def classroom_performance(classroom) -> dict:
    """How one classroom has done across every round it has played."""
    answers = Answer.objects.filter(classroom=classroom)

    aggregates = answers.aggregate(
        correct=Count("id", filter=Q(is_correct=True)),
        given=Count("id"),
        score=Sum(F("points")) + Sum(F("speed_bonus")),
        response_total=Sum("response_time_seconds"),
        competitions=Count("competition_id", distinct=True),
    )

    competitions = list(
        Answer.objects.filter(classroom=classroom)
        .values("competition_id")
        .annotate(score=Sum(F("points")) + Sum(F("speed_bonus")))
        .order_by("-competition_id")
    )

    played = len(competitions)
    total = int(aggregates["score"] or 0)

    return {
        "classroom": classroom,
        "competitions_played": played,
        "answers_given": aggregates["given"] or 0,
        "correct_answers": aggregates["correct"] or 0,
        "wrong_answers": (aggregates["given"] or 0) - (aggregates["correct"] or 0),
        "accuracy": _accuracy(aggregates["correct"] or 0, aggregates["given"] or 0),
        "total_score": total,
        "average_score": round(total / played, 1) if played else None,
        "average_response_seconds": (
            round(float(aggregates["response_total"] or 0.0) / aggregates["given"], 2)
            if aggregates["given"]
            else None
        ),
        "per_competition": [
            {"competition_id": row["competition_id"], "score": int(row["score"] or 0)}
            for row in competitions
        ],
    }


def question_statistics(question) -> dict:
    """How often a question has been asked, and how well it went.

    Answered once per question in this shape, so the figures are the same as those
    in a competition report - a question used in three rounds gets one row here
    covering all three.
    """
    answers = Answer.objects.filter(question=question)

    aggregates = answers.aggregate(
        given=Count("id"),
        correct=Count("id", filter=Q(is_correct=True)),
        response_total=Sum("response_time_seconds"),
        uses=Count("competition_id", distinct=True),
    )

    given = aggregates["given"] or 0
    return {
        "question": question,
        "question_type": question.question_type,
        "question_type_display": question.get_question_type_display(),
        "times_used": aggregates["uses"] or 0,
        "times_asked": given,
        "correct_answers": aggregates["correct"] or 0,
        "wrong_answers": given - (aggregates["correct"] or 0),
        "correct_percentage": _accuracy(aggregates["correct"] or 0, given),
        "average_response_seconds": (
            round(float(aggregates["response_total"] or 0.0) / given, 2) if given else None
        ),
    }


def tournament_report(tournament) -> dict:
    """A championship: its stages, its classrooms, its finalists and its result.

    Assembled by delegating to :func:`competition_report` for each round, so a
    tournament report and a competition report cannot print different numbers for
    the same round.
    """
    from tournaments import services as tournament_services

    history = tournament_services.history_for(tournament)

    stage_rows = []
    for entry in history["stages"]:
        competitions = [
            competition_report(competition)
            for competition in entry["competitions"]
        ]
        stage_rows.append(
            {
                "stage": entry["stage"],
                "competitions": competitions,
                "result": entry["result"],
            }
        )

    participants = [
        {
            "participant": participant,
            "classroom": participant.classroom.display_name,
            "status": participant.status,
            "status_display": participant.get_status_display(),
            "aggregate_score": participant.aggregate_score,
            "eliminated_at": participant.eliminated_at_stage,
        }
        for participant in history["participants"]
    ]

    record = history["record"]
    standings = record.rows if record else []
    finalists = [
        row for row in standings if row.get("status") in {"qualified", "champion"}
    ]

    return {
        "tournament": tournament,
        "season": history["season"],
        "stages": stage_rows,
        "participants": participants,
        "standings": standings,
        "finalists": finalists,
        "record": record,
        "winner": record.winner_name if record else "",
    }


def competition_options():
    """Finished competitions a report may be asked for, newest first."""
    return Competition.objects.filter(
        state=CompetitionState.FINISHED
    ).order_by("-finished_at", "-pk")


__all__ = [
    "classroom_performance",
    "competition_options",
    "competition_report",
    "question_statistics",
    "tournament_report",
]
