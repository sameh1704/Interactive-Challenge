"""The live leaderboard.

Ranking is computed from stored answers on every read rather than kept in a
counter column. A running total that is updated as answers arrive is faster, but
it is also a second source of truth that can disagree with the answers it was
derived from - and when a duplicate is refused or an answer is corrected, the
leaderboard would be wrong until someone noticed. Deriving it means the board is
always consistent with the record by construction.

Every participating classroom appears, including one that has not answered yet.
A class sitting at zero is information the room needs: it is why the board shows
a score at all.

Ordering
--------
Score, then correct answers, then total response time, then name. Each tie-break
only decides between classrooms that are genuinely level on everything before it,
so the order is total and stable - the same answers always produce the same
ranking, which matters when a result is announced to a room.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, F, Q, Sum

from scoring.models import Answer


@dataclass(frozen=True)
class LeaderboardRow:
    """One classroom's standing. Immutable, so it can be broadcast as-is."""

    rank: int
    classroom_id: int
    classroom_name: str
    score: int
    correct_answers: int
    answers_given: int
    questions_asked: int
    average_response_seconds: float | None = None

    @property
    def unanswered(self) -> int:
        return max(0, self.questions_asked - self.answers_given)

    def as_snapshot(self) -> dict:
        """The stored form, used by ``CompetitionResult``."""
        return {
            "rank": self.rank,
            "classroom_id": self.classroom_id,
            "classroom": self.classroom_name,
            "score": self.score,
            "correct_answers": self.correct_answers,
            "answers_given": self.answers_given,
            "questions_asked": self.questions_asked,
            "average_response_seconds": self.average_response_seconds,
        }

    def as_payload(self) -> dict:
        """The wire form, used by the leaderboard WebSocket."""
        return {
            "rank": self.rank,
            "classroom_id": self.classroom_id,
            "classroom": self.classroom_name,
            "score": self.score,
            "correct_answers": self.correct_answers,
            "answers_given": self.answers_given,
            "questions_asked": self.questions_asked,
            "average_response_seconds": self.average_response_seconds,
        }


def leaderboard_rows(competition) -> list[LeaderboardRow]:
    """Rank every classroom taking part in ``competition``.

    One aggregate query for the answers, one for the classrooms. A round has a
    handful of rooms, so this stays cheap enough to run on every reveal and on
    every leaderboard page load without caching it.
    """
    totals: dict[int, dict] = {}

    aggregates = (
        Answer.objects.for_competition(competition)
        .values("classroom_id")
        .annotate(
            score=Sum(F("points")) + Sum(F("speed_bonus")),
            correct_answers=Count("id", filter=Q(is_correct=True)),
            answers_given=Count("id"),
            response_total=Sum("response_time_seconds"),
        )
    )

    for row in aggregates:
        answered = row["answers_given"] or 0
        totals[row["classroom_id"]] = {
            "score": int(row["score"] or 0),
            "correct_answers": row["correct_answers"] or 0,
            "answers_given": answered,
            "response_total": float(row["response_total"] or 0.0),
        }

    participating = list(
        competition.classrooms.select_related("classroom").order_by(
            "classroom__name"
        )
    )

    rows: list[LeaderboardRow] = []
    for entry in participating:
        classroom = entry.classroom
        figures = totals.get(
            classroom.pk,
            {"score": 0, "correct_answers": 0, "answers_given": 0, "response_total": 0.0},
        )
        answered = figures["answers_given"]
        rows.append(
            LeaderboardRow(
                rank=0,
                classroom_id=classroom.pk,
                classroom_name=classroom.name,
                score=figures["score"],
                correct_answers=figures["correct_answers"],
                answers_given=answered,
                questions_asked=competition.total_questions,
                average_response_seconds=(
                    round(figures["response_total"] / answered, 2) if answered else None
                ),
            )
        )

    rows.sort(key=_ranking_key)
    return _with_ranks(rows)


def _ranking_key(row: LeaderboardRow):
    """Sort key implementing the documented ordering.

    Response time is a tie-break only. It must never outrank score, or a class
    that answered quickly and wrongly would beat one that answered slowly and
    correctly.
    """
    return _level_key(row) + (row.classroom_name.lower(),)


def _level_key(row: LeaderboardRow):
    """The criteria a class is actually ranked on.

    Excludes the name, which exists only to make the ordering total and
    repeatable. Two classes level on everything measurable here genuinely share a
    rank, and a results board that showed them as 1st and 2nd would be claiming a
    difference that the scoring did not produce.
    """
    return (
        -row.score,
        -row.correct_answers,
        # A class that has answered nothing has no average; it sorts last
        # within its bracket rather than being treated as infinitely fast.
        row.average_response_seconds
        if row.average_response_seconds is not None
        else float("inf"),
    )


def _with_ranks(rows: list[LeaderboardRow]) -> list[LeaderboardRow]:
    """Assign competition ranks: 1, 2, 2, 4.

    Equal standings share a rank and the next rank skips accordingly, which is
    what a results board is expected to show.
    """
    ranked: list[LeaderboardRow] = []
    previous = None
    current_rank = 0

    for index, row in enumerate(rows, start=1):
        level = _level_key(row)
        if previous is None or level != previous:
            current_rank = index
            previous = level
        ranked.append(
            LeaderboardRow(
                rank=current_rank,
                classroom_id=row.classroom_id,
                classroom_name=row.classroom_name,
                score=row.score,
                correct_answers=row.correct_answers,
                answers_given=row.answers_given,
                questions_asked=row.questions_asked,
                average_response_seconds=row.average_response_seconds,
            )
        )
    return ranked


def leaderboard_payload(competition) -> dict:
    """The leaderboard as a broadcast/screen payload."""
    from django.utils import timezone

    rows = leaderboard_rows(competition)
    return {
        "competition_id": competition.pk,
        "competition_title": competition.title,
        "state": competition.state,
        "question_number": competition.question_number,
        "total_questions": competition.total_questions,
        "server_time": timezone.now().isoformat(),
        "rows": [row.as_payload() for row in rows],
    }


def answer_progress(competition) -> dict:
    """How many classrooms have answered the open question.

    Safe to broadcast while answering is still open, because a count reveals
    nothing about correctness - which is exactly why the scored leaderboard is
    not broadcast until the reveal.
    """
    answered = (
        Answer.objects.for_competition(competition)
        .filter(position=competition.question_number)
        .count()
    )
    return {
        "competition_id": competition.pk,
        "position": competition.question_number,
        "answers_count": answered,
        "classes_count": competition.classrooms.count(),
    }


__all__ = [
    "LeaderboardRow",
    "answer_progress",
    "leaderboard_payload",
    "leaderboard_rows",
]