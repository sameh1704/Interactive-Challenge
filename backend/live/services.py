"""Competition control: the only code allowed to move a competition's state.

Keeping every transition in one synchronous service - rather than inside the
consumers - gives three things that matter operationally:

* **One definition of "now".** ``started_at`` and ``ends_at`` are written once,
  here, and every event carries them.
* **Testability without a socket.** Transitions can be tested as plain function
  calls; the consumers only translate between JSON and these calls.
* **A single choke point for authorisation.** A screen cannot reach this module
  at all, and a teacher who is not the owner is refused by
  :func:`assert_can_control`.

Scoring is deliberately absent (project rule 14, and the phase brief). This
module moves the round between questions; it does not award marks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from competitions.models import Competition, CompetitionState
from questions.models import Question

logger = logging.getLogger(__name__)


class CompetitionControlError(Exception):
    """A control action was refused. The message is safe to show a teacher."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Transition:
    """What changed, so callers can build the right broadcast."""

    competition: Competition
    event: str
    payload: dict


def assert_can_control(competition: Competition, user) -> None:
    """Refuse anyone who may not drive this round.

    The owning teacher may always control their own round. An administrator may
    take over, because during a live lesson an administrator is the person most
    likely to be standing at the laptop. Anyone else is refused.
    """
    from accounts.permissions import is_administrator

    if not user or not user.is_authenticated or not user.is_active:
        raise CompetitionControlError(
            "not_signed_in", "Sign in to run a competition."
        )
    if competition.teacher_id == user.pk or is_administrator(user):
        return
    raise CompetitionControlError(
        "not_authorised", "You are not the teacher running this competition."
    )


def assert_has_questions(competition: Competition) -> None:
    if not competition.questions.exists():
        raise CompetitionControlError(
            "no_questions", "Add at least one question before starting."
        )


@transaction.atomic
def start_competition(competition: Competition) -> Transition:
    """Start the round and broadcast ``competition_started``.

    Re-starting a round that is already running is refused rather than silently
    re-broadcast, so a double-clicked button cannot reset a live competition.
    """
    if competition.started_at is not None:
        raise CompetitionControlError(
            "already_started", "This competition has already been started."
        )
    if not competition.classrooms.exists():
        raise CompetitionControlError(
            "no_classrooms",
            "Include at least one classroom before starting.",
        )
    assert_has_questions(competition)

    now = timezone.now()
    competition.state = CompetitionState.WAITING
    competition.started_at = now
    competition.total_questions = competition.questions.count()
    competition.save(
        update_fields=[
            "state",
            "started_at",
            "total_questions",
            "updated_at",
        ]
    )

    return Transition(
        competition=competition,
        event="competition_started",
        payload={
            "competition_id": competition.pk,
            "competition_title": competition.title,
            "state": competition.state,
            "server_time": now.isoformat(),
            "total_questions": competition.total_questions,
            "started_at": competition.started_at.isoformat(),
        },
    )


@transaction.atomic
def start_question(competition: Competition, position: int | None = None) -> Transition:
    """Put a question on screen and broadcast ``question_started``.

    With no ``position`` this advances to the next question in order. Positions
    are looked up in the database, so a client cannot ask for a question that is
    not part of this round.
    """
    if competition.started_at is None:
        raise CompetitionControlError(
            "not_started", "Start the competition before asking a question."
        )
    if competition.is_finished:
        raise CompetitionControlError("finished", "This competition has finished.")

    if position is None:
        entry = (
            competition.questions.filter(position__gt=competition.question_number)
            .order_by("position")
            .first()
        )
        if entry is None:
            raise CompetitionControlError(
                "no_next_question", "There is no further question in this round."
            )
    else:
        entry = competition.questions.filter(position=position).first()
        if entry is None:
            raise CompetitionControlError(
                "unknown_position", f"No question at position {position}."
            )

    now = timezone.now()
    duration = entry.effective_duration_seconds

    competition.current_question = entry.question
    competition.question_number = entry.position
    competition.current_question_started_at = now
    competition.current_question_ends_at = now + timedelta(seconds=duration)
    competition.state = CompetitionState.QUESTION_ACTIVE
    competition.save(
        update_fields=[
            "current_question",
            "question_number",
            "current_question_started_at",
            "current_question_ends_at",
            "state",
            "updated_at",
        ]
    )

    return Transition(
        competition=competition,
        event="question_started",
        payload=build_question_payload(competition, entry, now),
    )


def build_question_payload(competition: Competition, entry, now=None) -> dict:
    """Assemble the ``question_started`` payload.

    Re-read through :func:`live.events.question_payload` so that the HTTP-facing
    builder and the WebSocket builder cannot drift apart.
    """
    from live.events import question_payload

    return question_payload(competition, entry, server_now=now or timezone.now())


@transaction.atomic
def close_answering(competition: Competition, *, reason: str = "time_expired") -> Transition:
    """Close the open answer period and broadcast ``answering_closed``.

    The single transition that ends a question. Both callers reach it through
    here: the server clock when the deadline passes, and :func:`end_question` when
    a teacher presses the button. They differ only in ``reason``, which is logged
    and never broadcast.

    That last point is deliberate. A screen receiving the event cannot tell
    whether the teacher ran out of patience or the room was too slow, and that
    is the right outcome: the fact that a teacher pressed early is not something
    a class of pupils should be able to read off the wire. Both paths produce a
    byte-identical payload.
    """
    if competition.state != CompetitionState.QUESTION_ACTIVE:
        raise CompetitionControlError(
            "not_answering", "No question is currently open for answers."
        )

    competition.state = CompetitionState.ANSWERING_CLOSED
    competition.save(update_fields=["state", "updated_at"])

    logger.info(
        "Competition %s closed answering on position %s (%s).",
        competition.pk,
        competition.question_number,
        reason,
    )

    now = timezone.now()
    return Transition(
        competition=competition,
        event="answering_closed",
        payload={
            "competition_id": competition.pk,
            "state": competition.state,
            "server_time": now.isoformat(),
            "question_number": competition.question_number,
            "seconds_remaining": 0.0,
        },
    )


def end_question(competition: Competition) -> Transition:
    """The teacher's *End Question* control: stop answering now.

    Ends the answer period immediately and stops there. It does **not** move on to
    the next question, because a question that ends early is a question whose
    result the teacher still wants to see. The full sequence is:

        QUESTION_ACTIVE -> ANSWERING_CLOSED -> (result is revealed) -> advance

    Every guard against doing this twice, or advancing mid-transition, lives in
    the state check inside :func:`close_answering`; this function exists so the
    teacher's action is a named, documented control rather than a second
    implementation of the same transition.
    """
    return close_answering(competition, reason="teacher_ended")


# States from which the teacher may move the round on. Notably absent is
# QUESTION_ACTIVE: skipping a question that is still open would strand every
# classroom that has not answered yet, and would never produce the result for the
# question that was skipped.
ADVANCE_ALLOWED_STATES = frozenset(
    {
        CompetitionState.WAITING,
        CompetitionState.ANSWERING_CLOSED,
        CompetitionState.SHOWING_RESULT,
        CompetitionState.NEXT_QUESTION,
    }
)


@transaction.atomic
def advance(competition: Competition) -> Transition:
    """Teacher-initiated move to the next question, or to the result view.

    Only valid once the current question is closed, so the sequence a teacher
    sees is always: question on screen, result revealed, next question. Starting
    the next question while answers are still being accepted is refused rather
    than honoured, because it would end the question without a result.
    """
    if competition.started_at is None:
        raise CompetitionControlError(
            "not_started", "Start the competition before advancing."
        )
    if competition.is_finished:
        raise CompetitionControlError(
            "finished", "This competition has already finished."
        )
    if competition.state not in ADVANCE_ALLOWED_STATES:
        raise CompetitionControlError(
            "question_still_open",
            "End the question first: answers are still being accepted.",
        )

    next_entry = (
        competition.questions.filter(position__gt=competition.question_number)
        .order_by("position")
        .first()
    )
    if next_entry is not None:
        return start_question(competition, position=next_entry.position)

    competition.state = CompetitionState.NEXT_QUESTION
    competition.save(update_fields=["state", "updated_at"])

    now = timezone.now()
    return Transition(
        competition=competition,
        event="next_question",
        payload={
            "competition_id": competition.pk,
            "state": competition.state,
            "server_time": now.isoformat(),
            "question_number": competition.question_number,
            "total_questions": competition.total_questions,
            "has_next": False,
        },
    )


@transaction.atomic
def finish_competition(competition: Competition) -> Transition:
    """End the round."""
    competition.state = CompetitionState.FINISHED
    competition.finished_at = timezone.now()
    competition.current_question_ends_at = competition.finished_at
    competition.save(
        update_fields=["state", "finished_at", "current_question_ends_at", "updated_at"]
    )

    return Transition(
        competition=competition,
        event="competition_finished",
        payload={
            "competition_id": competition.pk,
            "state": competition.state,
            "server_time": competition.finished_at.isoformat(),
            "finished_at": competition.finished_at.isoformat(),
        },
    )


def expired(competition: Competition, now=None) -> bool:
    """Whether the server clock has passed the current question's deadline.

    Read by each screen consumer's tick loop, so the deadline is enforced once
    per competition rather than once per connected screen.
    """
    if competition.current_question_ends_at is None:
        return False
    now = now or timezone.now()
    return now >= competition.current_question_ends_at
