"""Broadcasting scoring results.

Kept apart from :mod:`scoring.services` so the scoring rules stay testable and
reusable without a channel layer. Nothing here decides anything; it only
publishes what the services already decided.

What may be broadcast, and when
--------------------------------
The correctness of an answer is secret until the answer period ends. That single
rule governs this module:

* While a question is open, only :func:`broadcast_answer_progress` is sent - a
  count of who has answered. A count cannot reveal who was right.
* When answering closes, :func:`broadcast_result_revealed` sends the answer key,
  the per-classroom outcomes, and the leaderboard.
* When a round finishes, :func:`broadcast_final_results` sends the frozen
  standings.

The scored leaderboard is deliberately *not* sent while a question is open.
Two classes on different scores during a question implies which of them is
correct, so publishing it early would leak the key through arithmetic.
"""

from __future__ import annotations

import logging

from channels.db import database_sync_to_async
from django.utils import timezone

from live import groups
from scoring.leaderboard import answer_progress, leaderboard_payload
from scoring.services import finalise, question_breakdown

logger = logging.getLogger(__name__)


async def broadcast_answer_progress(channel_layer, competition_id: int) -> None:
    """Tell every screen how many classrooms have answered, and nothing more.

    Takes an id rather than an object because the caller is a consumer holding
    the id of the round it is attached to, not a loaded competition.
    """
    progress = await _progress(competition_id)
    if progress is None:
        return

    await _to_screen_group(channel_layer, competition_id, progress)


async def broadcast_result_revealed(channel_layer, competition_id: int) -> dict:
    """Publish the answer key, the per-question outcomes and the leaderboard.

    Called when answering closes. Today the server clock is the only thing that
    closes it, which keeps the rule simple to state and to test: no classroom is
    ever shown an outcome it has not earned, and nothing about how the deadline
    was reached leaks into what it sees. A teacher control that ends a question
    early would publish through here too, and would have to publish exactly
    this, because a screen cannot tell which of the two happened.
    """
    payload = await _reveal_payload(competition_id)
    if payload is None:
        return {}

    await _broadcast_all(channel_layer, competition_id, payload)
    return payload


async def broadcast_leaderboard(channel_layer, competition_id: int) -> None:
    """Publish the standings without an answer key.

    Used by the teacher finishing a round, where the key is no longer a secret
    and the board is what actually matters.
    """
    payload = await _leaderboard(competition_id)
    if payload is None:
        return

    await _broadcast_all(channel_layer, competition_id, payload)


async def broadcast_final_results(channel_layer, competition_id: int) -> dict:
    """Freeze and publish the final standings of a finished round."""
    result = await _finalise(competition_id)
    if result is None:
        return {}

    payload = {
        "type": "competition_results",
        **result,
    }
    await _broadcast_all(channel_layer, competition_id, payload)
    return payload


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


async def _to_screen_group(channel_layer, competition_id: int, payload: dict) -> None:
    await channel_layer.group_send(
        groups.screen_group(competition_id), {"type": payload["type"], "payload": payload}
    )


async def _broadcast_all(channel_layer, competition_id: int, payload: dict) -> None:
    """Send to screens, teachers and the leaderboard display together.

    The leaderboard group exists because a room often shows the standings on a
    separate display that no teacher is signed in to.
    """
    message = {"type": payload["type"], "payload": payload}
    await channel_layer.group_send(groups.screen_group(competition_id), message)
    await channel_layer.group_send(groups.teacher_group(competition_id), message)
    await channel_layer.group_send(groups.leaderboard_group(competition_id), message)


@database_sync_to_async
def _progress(competition_id: int):
    from competitions.models import Competition

    competition = Competition.objects.filter(pk=competition_id).first()
    if competition is None:
        return None
    return {"type": "answer_progress", **answer_progress(competition)}


@database_sync_to_async
def _reveal_payload(competition_id: int):
    from competitions.models import Competition

    competition = (
        Competition.objects.filter(pk=competition_id)
        .select_related("current_question")
        .first()
    )
    if competition is None:
        return None

    return {
        "type": "result_revealed",
        **question_breakdown(competition),
        "leaderboard": leaderboard_payload(competition),
        "server_time": timezone.now().isoformat(),
    }


@database_sync_to_async
def _leaderboard(competition_id: int):
    from competitions.models import Competition

    competition = Competition.objects.filter(pk=competition_id).first()
    if competition is None:
        return None
    return {"type": "leaderboard_updated", **leaderboard_payload(competition)}


@database_sync_to_async
def _finalise(competition_id: int):
    from competitions.models import Competition

    competition = Competition.objects.filter(pk=competition_id).first()
    if competition is None:
        return None

    result = finalise(competition)
    return {
        "competition_id": competition.pk,
        "competition_title": competition.title,
        "standings": result.rows,
        "total_questions": result.total_questions,
        "answered_count": result.answered_count,
        "winner": result.winner_name,
        "server_time": timezone.now().isoformat(),
    }


__all__ = [
    "broadcast_answer_progress",
    "broadcast_final_results",
    "broadcast_leaderboard",
    "broadcast_result_revealed",
]