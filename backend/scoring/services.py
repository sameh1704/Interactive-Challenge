"""Answer submission, scoring and results.

This module is the whole of Phase 5's business logic, and it is deliberately
free of WebSockets, HTTP and templates. A screen's consumer calls
:func:`submit_answer`; a test calls it directly; the leaderboard screen renders
what :mod:`scoring.leaderboard` returns. None of those depend on each other.

The authority rule
------------------
Every value that decides an outcome comes from the server. Concretely,
:func:`submit_answer` accepts only *what the classroom chose*; it derives:

``classroom``
    from the caller's already-authenticated screen, not from the message;
``question``
    from the competition's current question, not from the message;
``submitted_at``
    from ``timezone.now()`` at receipt, not from a client timestamp;
``response_time_seconds``
    from ``submitted_at - competition.current_question_started_at``;
``is_correct``, ``points``, ``speed_bonus``
    from the stored question key and the stored scoring rule.

A message that also carries ``score``, ``is_correct``, ``response_time`` or
``classroom_id`` is not rejected - rejecting it would teach a client which fields
matter - but those keys are simply never read. See
:func:`extract_selection`, which is the only thing taken from the message.

Duplicate and late answers
--------------------------
Both are refused, and both refusals are database-backed rather than checked-then-
written: the unique constraint on (competition, question, classroom) means two
concurrent submissions cannot both land, even though each passed its own check.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from competitions.models import CompetitionState
from scoring.models import (
    Answer,
    CompetitionResult,
    ScoringRule,
    ScoringRuleValues,
    SpeedBonusMode,
)

# Keys a client must never be able to influence. Named so the intent is checkable
# rather than merely documented.
UNTRUSTED_KEYS = (
    "score",
    "points",
    "speed_bonus",
    "total_score",
    "is_correct",
    "correct",
    "correctness",
    "response_time",
    "response_time_seconds",
    "submitted_at",
    "classroom_id",
    "classroom",
    "screen_id",
    "competition_id",
    "question_id",
    "position",
)


class AnswerRejected(Exception):
    """An answer was refused. ``code`` is safe to return to a screen."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ScoreBreakdown:
    """The arithmetic behind one answer, kept separate so it can be tested alone."""

    points: int
    speed_bonus: int
    is_correct: bool

    @property
    def total(self) -> int:
        return self.points + self.speed_bonus


# ---------------------------------------------------------------------------
# Pure scoring
# ---------------------------------------------------------------------------


def rule_values_for(competition) -> ScoringRuleValues:
    """The scoring rule in force for a competition.

    Falls back to the configured defaults when the competition has no rule row,
    so a round set up with no scoring configuration still scores correctly.
    """
    rule = getattr(competition, "scoring_rule", None)
    if rule is None:
        return ScoringRule.defaults()
    return ScoringRuleValues(
        correct_points=rule.correct_points,
        speed_bonus_max=rule.speed_bonus_max,
        speed_bonus_mode=rule.speed_bonus_mode,
    )


def speed_bonus_for(
    response_time_seconds: float,
    duration_seconds: int,
    rule: ScoringRuleValues,
) -> int:
    """Bonus points for answering quickly, never negative and never over the cap.

    Linear decay: the whole bonus is available at the instant the question
    appears and decays to zero by the deadline. Decaying rather than
    all-or-nothing keeps a pupil who was nearly right in a race for the point,
    which is the point of a speed bonus.

    An answer at or after the deadline earns nothing, and a zero-length question
    cannot divide by zero - it simply pays the full bonus to whoever answers
    first, which is the only sensible reading.
    """
    if rule.speed_bonus_mode == SpeedBonusMode.NONE or rule.speed_bonus_max <= 0:
        return 0

    elapsed = max(0.0, float(response_time_seconds))
    duration = float(duration_seconds)

    if duration <= 0:
        return int(rule.speed_bonus_max)

    remaining_fraction = 1.0 - (elapsed / duration)
    if remaining_fraction <= 0:
        return 0

    return int(round(rule.speed_bonus_max * min(remaining_fraction, 1.0)))


def score_answer(
    *,
    is_correct: bool,
    response_time_seconds: float,
    duration_seconds: int,
    rule: ScoringRuleValues,
) -> ScoreBreakdown:
    """Turn a known outcome into points. No database, no clock, no I/O.

    An incorrect answer scores nothing at all, including no speed bonus: paying
    a fast wrong answer would reward the guessing this system exists to
    discourage.
    """
    if not is_correct:
        return ScoreBreakdown(points=0, speed_bonus=0, is_correct=False)

    bonus = speed_bonus_for(response_time_seconds, duration_seconds, rule)
    return ScoreBreakdown(
        points=rule.correct_points,
        speed_bonus=bonus,
        is_correct=True,
    )


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


def extract_selection(message: dict) -> str | list | dict:
    """Read the one thing a client is allowed to state: what it chose.

    Accepts ``answer`` (the documented field) and ``selected_answer``, because a
    screen page and a teacher's script may reasonably use either. Every other
    key in the message is ignored by construction - this function is the only
    place a client-supplied value reaches the scoring path.

    The return type follows the question's type, because three of the question
    types are not answered by choosing a single value: an ordering answer is a
    sequence and a classification answer is a set of placements. Those are
    passed through here and checked structurally by
    :meth:`questions.models.Question.accepts`, which is the component that knows
    what shape the open question expects.

    Nothing here interprets the value. Unwrapping a one-element list and turning
    a boolean into ``"true"``/``"false"`` exist for the two legacy types, whose
    only answer is one value; a multi-element list is left intact because for an
    ordering question it *is* the answer.
    """
    for key in ("answer", "selected_answer"):
        if key in message:
            value = message[key]
            break
    else:
        raise AnswerRejected("missing_answer", "Send the answer you chose.")

    if isinstance(value, list) and len(value) == 1:
        value = value[0]

    if isinstance(value, bool) or value is None:
        value = "true" if value else "false"

    if isinstance(value, dict):
        placements = {}
        for label, category in value.items():
            if not isinstance(label, str) or not label.strip():
                raise AnswerRejected("invalid_answer", "Send an item to place.")
            if not isinstance(category, str) or not category.strip():
                raise AnswerRejected("invalid_answer", "Send a category for each item.")
            placements[label.strip()] = category.strip()
        if not placements:
            raise AnswerRejected("missing_answer", "Send the answer you chose.")
        return placements

    if isinstance(value, list):
        if not value:
            raise AnswerRejected("missing_answer", "Send the answer you chose.")
        if all(isinstance(item, str) for item in value):
            return [item.strip() for item in value]
        if all(isinstance(item, dict) for item in value):
            # A classification answer expressed as a list of placements. Left
            # exactly as sent so `Question.accepts` can reject it if it does not
            # name real items or real categories.
            return list(value)
        raise AnswerRejected("invalid_answer", "Send the answer as a list.")

    if not isinstance(value, str):
        raise AnswerRejected("invalid_answer", "Send the answer you chose.")

    text = value.strip()
    if not text:
        raise AnswerRejected("missing_answer", "Send the answer you chose.")
    return text


@transaction.atomic
def submit_answer(
    *,
    competition,
    classroom,
    screen=None,
    selection: str,
    now=None,
) -> Answer:
    """Record and score one classroom's answer to the open question.

    ``selection`` must already have come from :func:`extract_selection`. The
    remaining arguments are resolved by the caller from authenticated state, so
    there is no way for a client to name a different classroom, question or time.
    """
    now = now or timezone.now()

    question = competition.current_question
    if question is None:
        raise AnswerRejected("no_question", "There is no question to answer.")
    if competition.is_finished:
        raise AnswerRejected("finished", "This competition has finished.")
    if competition.state != CompetitionState.QUESTION_ACTIVE:
        raise AnswerRejected(
            "not_answering", "Answers are not being accepted right now."
        )

    if classroom is None:
        raise AnswerRejected("unknown_classroom", "This screen has no classroom.")
    if not competition.includes_classroom(classroom.pk):
        # Belt and braces: the live engine already refuses such a screen at
        # connect time, but scoring must not depend on that having happened.
        raise AnswerRejected(
            "classroom_not_included",
            "This classroom is not taking part in this competition.",
        )

    if not question.accepts(selection):
        raise AnswerRejected(
            "invalid_answer", "That is not one of the available answers."
        )

    if not question.has_answer_key:
        raise AnswerRejected(
            "no_answer_key", "This question has no answer key, so it cannot be scored."
        )

    ends_at = competition.current_question_ends_at
    if ends_at is not None and now > ends_at:
        raise AnswerRejected(
            "late_answer", "The time for this question has already ended."
        )

    started_at = competition.current_question_started_at
    if started_at is None:
        raise AnswerRejected(
            "not_answering", "Answers are not being accepted right now."
        )

    entry = competition.questions.filter(question=question).first()
    if entry is None:
        raise AnswerRejected(
            "wrong_question", "That question is not part of this competition."
        )
    duration = entry.effective_duration_seconds

    response_time = max(0.0, (now - started_at).total_seconds())
    # The only decision about correctness, and it is made here against the stored
    # key. The client never contributes to it beyond the choice it already made.
    is_correct = question.matches(selection)
    key = question.answer_key
    breakdown = score_answer(
        is_correct=is_correct,
        response_time_seconds=response_time,
        duration_seconds=duration,
        rule=rule_values_for(competition),
    )

    try:
        return Answer.objects.create(
            competition=competition,
            question=question,
            position=entry.position,
            classroom=classroom,
            screen=screen,
            selected_answer=selection,
            submitted_at=now,
            response_time_seconds=round(response_time, 3),
            duration_seconds=duration,
            is_correct=breakdown.is_correct,
            points=breakdown.points,
            speed_bonus=breakdown.speed_bonus,
            correct_answer=key,
        )
    except IntegrityError as exc:
        # Two classrooms submitting at once is normal; the same classroom twice
        # is not. The unique constraint is what tells them apart, and this is
        # the only place a race between two sockets can be resolved correctly.
        if "one_answer_per_classroom_question" in str(exc):
            raise AnswerRejected(
                "already_answered", "This classroom has already answered."
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Revealing and finishing
# ---------------------------------------------------------------------------


def question_breakdown(competition, position: int | None = None) -> dict:
    """Per-classroom outcome for one question, for the reveal payload.

    Only ever called after answering has closed - the caller in
    :mod:`scoring.realtime` is what enforces that. Returns the answer key, which
    is why it must not be built any earlier.
    """
    position = position or competition.question_number
    answers = list(
        Answer.objects.for_competition(competition)
        .filter(position=position)
        .select_related("classroom", "question")
        .order_by("classroom__name")
    )

    question = competition.current_question

    # The key is assembled for display from the question that was actually
    # answered, not from `competition.current_question_id` alone: a report of
    # question 3 must describe question 3 even if question 4 is now on screen.
    breakdown = _breakdown_for(competition, position, question)

    return {
        "competition_id": competition.pk,
        "position": position,
        "question_id": question.pk if question else None,
        "question_type": question.question_type if question else None,
        "correct_answer": breakdown["correct_answer"],
        "explanation": breakdown["explanation"],
        "answers_count": len(answers),
        "classes_count": competition.classrooms.count(),
        "answers": [
            {
                "classroom_id": answer.classroom_id,
                "classroom": answer.classroom.name,
                "selected_answer": answer.selection,
                "is_correct": answer.is_correct,
                "response_time_seconds": answer.response_time_seconds,
                "points": answer.points,
                "speed_bonus": answer.speed_bonus,
                "total_score": answer.total_score,
            }
            for answer in answers
        ],
    }


def _breakdown_for(competition, position: int, question) -> dict:
    """The answer key and explanation for one position, if they can be recovered.

    Prefers the question object, and falls back to the key recorded on each
    answer - which is the whole reason that key was stored, so a report stays
    readable even for a question that has since been edited.
    """
    if question is not None:
        return {
            "correct_answer": question.answer_key,
            "explanation": question.explanation or None,
        }

    recorded = (
        Answer.objects.for_competition(competition)
        .filter(position=position)
        .exclude(correct_answer="")
        .values_list("correct_answer", flat=True)
        .first()
    )
    return {"correct_answer": recorded or None, "explanation": None}


@transaction.atomic
def finalise(competition, now=None) -> CompetitionResult:
    """Freeze the standings of a finished round.

    Idempotent: finishing a round twice, or re-running it after a correction,
    updates the existing snapshot rather than creating a second one. That keeps
    ``Competition.result`` a genuine one-to-one.
    """
    from scoring.leaderboard import leaderboard_rows

    now = now or timezone.now()
    rows = leaderboard_rows(competition)

    # A round in which nobody scored has no winner. Naming the first classroom
    # alphabetically would announce a result to a room that was not earned.
    leader = rows[0] if rows and rows[0].score > 0 else None

    result, _ = CompetitionResult.objects.update_or_create(
        competition=competition,
        defaults={
            "standings": [row.as_snapshot() for row in rows],
            "total_questions": competition.total_questions,
            "answered_count": Answer.objects.for_competition(competition).count(),
            "winner_name": leader.classroom_name if leader else "",
            "generated_at": now,
        },
    )
    return result


__all__ = [
    "AnswerRejected",
    "ScoreBreakdown",
    "UNTRUSTED_KEYS",
    "extract_selection",
    "finalise",
    "question_breakdown",
    "rule_values_for",
    "score_answer",
    "speed_bonus_for",
    "submit_answer",
]