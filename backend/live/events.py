"""WebSocket event names and payload builders.

Every message the server sends is defined here, so the wire format has exactly
one description and both consumers and the browser scripts are tested against
it. Event names are deliberately verb-like and stable: a screen's JavaScript
switches on them, so renaming one is a breaking change.
"""

from __future__ import annotations

from django.utils import timezone

# -- server -> screen -------------------------------------------------------

# Sent once, immediately after a screen's identity is accepted. Tells the screen
# what it is and what competition it is currently attached to.
WELCOME = "welcome"

# The teacher has started the round.
COMPETITION_STARTED = "competition_started"

# A new question is on screen. Carries the full question payload.
QUESTION_STARTED = "question_started"

# The server clock has passed the question's end time.
ANSWERING_CLOSED = "answering_closed"

# The teacher has moved the round on to the next question.
NEXT_QUESTION = "next_question"

# The teacher has finished the round.
COMPETITION_FINISHED = "competition_finished"

# A screen's identity was refused. Sent before the socket closes.
REJECTED = "rejected"

# Regular clock beacon. Lets a browser correct its countdown against the server
# rather than trusting the local clock.
TICK = "tick"

# Full state, sent to a screen or teacher that (re)connects mid-round.
STATE_SYNC = "state_sync"

# A classroom has answered the open question. Carries only a count of answers
# received, because anything more would reveal who was right while the answer
# period is still open.
ANSWER_PROGRESS = "answer_progress"

# Answering has closed and the outcome of the question may be shown. This is the
# first event that may carry the answer key.
RESULT_REVEALED = "result_revealed"

# The standings, as they stand now. Carries scores, never an answer key.
LEADERBOARD_UPDATED = "leaderboard_updated"

# A finished round's frozen final standings.
COMPETITION_RESULTS = "competition_results"

# Sent to the screen that submitted an answer, to acknowledge receipt. Contains
# no correctness and no score: those are not known to the submitter.
ANSWER_ACCEPTED = "answer_accepted"

# -- server -> teacher ------------------------------------------------------

# How many screens are attached, and which classrooms they represent.
PRESENCE = "presence"

# The teacher pressed a control that is not currently allowed.
ERROR = "error"

# -- screen -> server -------------------------------------------------------

# A screen presents its Screen ID as soon as it connects.
IDENTIFY = "identify"

# A screen states which option its classroom chose. Only that value is read;
# see `scoring.services.extract_selection`.
SUBMIT_ANSWER = "submit_answer"

# -- teacher -> server ------------------------------------------------------

START_COMPETITION = "start_competition"
START_QUESTION = "start_question"

# The teacher has ended the open question before its time ran out. Produces the
# same ``answering_closed`` transition, the same broadcast and the same reveal as
# the deadline reaching zero - see ``live.services.end_question``.
END_QUESTION = "end_question"

# The teacher has moved the round on, having already seen the result.
ADVANCE = "advance"
FINISH_COMPETITION = "finish_competition"


def question_payload(
    competition,
    entry,
    server_now=None,
) -> dict:
    """Build the payload for ``question_started``.

    ``server_now`` and ``ends_at`` are ISO-8601 UTC strings. ``ends_at`` is what
    the countdown is derived from on every screen, so no screen has to trust its
    own clock - see ``live.clock.remaining_seconds``.
    """
    server_now = server_now or timezone.now()
    number = entry.position
    total = competition.total_questions

    payload = entry.question.as_live_payload(
        number=number,
        total=total,
        duration_seconds=entry.effective_duration_seconds,
    )
    payload.update(
        {
            "competition_id": competition.pk,
            "competition_title": competition.title,
            "state": competition.state,
            "starts_at": competition.current_question_started_at.isoformat(),
            "ends_at": competition.current_question_ends_at.isoformat(),
            "server_time": server_now.isoformat(),
            "duration_seconds": entry.effective_duration_seconds,
            "question_number": number,
            "total_questions": total,
        }
    )
    return payload


def state_payload(competition, screen=None, connected_screens=None) -> dict:
    """Build the payload for ``state_sync``.

    Sent when a client connects or reconnects so that it can render the current
    round immediately instead of waiting for the next event. A reconnecting
    screen therefore rejoins the same question with the correct remaining time.
    """
    now = timezone.now()
    payload = {
        "type": STATE_SYNC,
        "competition_id": competition.pk,
        "competition_title": competition.title,
        "state": competition.state,
        "question_number": competition.question_number,
        "total_questions": competition.total_questions,
        "server_time": now.isoformat(),
        "seconds_remaining": round(competition.seconds_remaining(now), 3),
        "started_at": competition.started_at.isoformat() if competition.started_at else None,
    }

    if connected_screens is not None:
        payload["connected_screens"] = connected_screens

    if competition.current_question_id:
        payload["question"] = competition.current_question.as_live_payload(
            number=competition.question_number,
            total=competition.total_questions,
            duration_seconds=competition.current_question.duration_seconds,
        )
        payload["question"].update(
            {
                "starts_at": competition.current_question_started_at.isoformat()
                if competition.current_question_started_at
                else None,
                "ends_at": competition.current_question_ends_at.isoformat()
                if competition.current_question_ends_at
                else None,
            }
        )

    if screen is not None:
        payload["screen"] = {
            "screen_id": screen.screen_id,
            "name": screen.name,
            "classroom": screen.classroom.display_name if screen.classroom else None,
        }

    return payload
