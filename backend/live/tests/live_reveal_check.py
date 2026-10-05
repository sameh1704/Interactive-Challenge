"""A real networked check of the reveal, run against Daphne and Redis.

The unit tests in this package drive the consumers in-process over the in-memory
channel layer, because ``channels_redis`` pools connections bound to one event
loop and Django gives each async test a fresh one. That is the right call for the
test suite, but it means the suite never proves the thing that actually matters
about a reveal: that it crosses a process boundary.

This script is that proof. It runs against a live Daphne serving ``config.asgi``
with the Redis channel layer, and its clients are ordinary WebSocket connections
over a TCP socket - the same path a classroom browser takes. Nothing here is a
mock: two labs really do connect, really do answer, and the answer key really does
appear on both sockets at the same moment when the deadline passes.

Run it inside the running container::

    docker compose exec -T challenge-web python -m live.tests.live_reveal_check

Exits non-zero on the first failed expectation. It creates and then deletes its
own data, so it can be run repeatedly against a development database.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402 - must follow the settings default

django.setup()

from asgiref.sync import async_to_sync  # noqa: E402
from channels.db import database_sync_to_async  # noqa: E402
from channels.layers import get_channel_layer  # noqa: E402
from django.utils import timezone  # noqa: E402

from core.tests.utils import (  # noqa: E402
    create_classroom,
    create_competition,
    create_question,
    create_screen,
    create_teacher,
)
from live import clock_runner, services  # noqa: E402
from live.tests.check_fixtures import (  # noqa: E402
    assert_no_residue,
    discard_check_fixtures,
)
from live.tests.wsclient import WebSocketClient  # noqa: E402

HOST = os.environ.get("LIVE_CHECK_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIVE_CHECK_PORT", "8000"))
CORRECT = "4"
WRONG = "5"


def ok(message: str) -> None:
    print(f"  PASS  {message}", flush=True)


def fail(message: str) -> None:
    print(f"  FAIL  {message}", flush=True)
    raise SystemExit(1)


def screen_client(label: str, screen_id: str) -> WebSocketClient:
    return WebSocketClient(label, f"/ws/live/screen/?screen_id={screen_id}", HOST, PORT)


def build_fixtures(tag: str) -> dict:
    """Create a two-lab, one-question competition, started and ready to run.

    ``tag`` is minted by ``main`` and passed in, so the same value bounds both
    the fixtures created here and the cleanup afterwards. Minting it inside this
    function meant a failure part way through left ``main`` with nothing to
    clean up by, because the tag died with the frame.
    """
    teacher = create_teacher(username=f"live.check.{tag}")
    lab_a = create_classroom(name=f"Science Lab A {tag}")
    lab_b = create_classroom(name=f"Science Lab B {tag}")
    question = create_question(
        text=f"What is 2 + 2? {tag}",
        options=["3", "4", "5", "6"],
        correct_option=CORRECT,
    )
    competition = create_competition(
        teacher=teacher,
        title=f"Reveal check {tag}",
        classrooms=[lab_a, lab_b],
        questions=[question],
    )
    services.start_competition(competition)
    services.start_question(competition, position=1)
    return {
        # The run's unique tag. `discard_check_fixtures` needs it to bound every
        # cleanup lookup, so it is part of the fixtures rather than a local.
        "tag": tag,
        "competition_id": competition.pk,
        "screen_a": create_screen(name=f"A board {tag}", classroom=lab_a).screen_id,
        "screen_b": create_screen(name=f"B board {tag}", classroom=lab_b).screen_id,
    }


def stored_answers(competition_id: int) -> list:
    from scoring.models import Answer

    return list(Answer.objects.for_competition(competition_id))


def stored_result(competition_id: int):
    from scoring.models import CompetitionResult

    return CompetitionResult.objects.filter(competition_id=competition_id).first()


async def expire_clock(competition_id: int, channel_layer) -> None:
    """Arm the server clock with a deadline already reached.

    A real question's clock, driven straight from a worker, does this on its own.
    Reaching for it directly keeps the check fast and makes the reveal's only
    trigger - the deadline - unambiguous.
    """
    await clock_runner.start_clock(
        competition_id, channel_layer, timezone.now() - timedelta(seconds=1)
    )


async def finish(competition_id: int, channel_layer) -> dict:
    from scoring.realtime import broadcast_final_results

    return await broadcast_final_results(channel_layer, competition_id)


def in_thread(func, *args, **kwargs):
    """Run a synchronous database call from outside any event loop."""
    return async_to_sync(database_sync_to_async(func))(*args, **kwargs)


async def query(func, *args, **kwargs):
    """The same, from inside the check's own event loop."""
    return await database_sync_to_async(func)(*args, **kwargs)


async def run_checks(fixtures: dict) -> int:
    channel_layer = get_channel_layer()
    competition_id = fixtures["competition_id"]

    fast = screen_client("lab A", fixtures["screen_a"])
    slow = screen_client("lab B", fixtures["screen_b"])
    await fast.connect()
    await slow.connect()
    try:
        await fast.wait_for("state_sync")
        await slow.wait_for("state_sync")
        ok("both labs connected over TCP and were welcomed")

        # An answer is accepted, and the acknowledgement tells the submitter only
        # that it was recorded - never whether it was right.
        await fast.send({"type": "submit_answer", "answer": CORRECT})
        accepted = await fast.wait_for("answer_accepted")
        if "is_correct" in accepted or "correct_answer" in accepted:
            fail(f"the acknowledgement leaked the key: {accepted}")
        ok("lab A's answer was accepted without being told it was correct")

        # The other lab answers wrong. Neither socket may learn the key yet.
        await slow.send({"type": "submit_answer", "answer": WRONG})
        await slow.wait_for("answer_accepted")
        progress = await slow.wait_for("answer_progress")
        if "correct_answer" in progress:
            fail(f"progress leaked the key before the reveal: {progress}")
        ok(f"lab B saw only a count while answering was open: {progress}")

        answers = await query(stored_answers, competition_id)
        if len(answers) != 2:
            fail(f"expected 2 stored answers, found {len(answers)}")
        ok("both answers are stored server-side while answering is still open")

        # Let the deadline pass, the way it would in a real question.
        await expire_clock(competition_id, channel_layer)
        print("  ....  the server clock passed the deadline", flush=True)

        reveal_a = await fast.wait_for("result_revealed")
        reveal_b = await slow.wait_for("result_revealed")
        if reveal_a.get("correct_answer") != CORRECT:
            fail(f"lab A's reveal carries the wrong key: {reveal_a}")
        if reveal_b.get("correct_answer") != CORRECT:
            fail(f"lab B's reveal carries the wrong key: {reveal_b}")
        if reveal_a.get("answers_count") != 2:
            fail(f"the reveal miscounts the answers: {reveal_a.get('answers_count')}")
        ok("both labs received the same answer key at the reveal")

        correct = [row for row in reveal_a["answers"] if row["is_correct"]]
        if len(correct) != 1:
            fail(f"exactly one lab should be correct: {reveal_a['answers']}")
        ok("the reveal reports which lab was right and which was wrong")

        rows = reveal_a["leaderboard"]["rows"]
        if len(rows) != 2 or rows[0]["score"] <= 0:
            fail(f"the reveal should carry a scored leaderboard: {rows}")
        ok(f"the reveal carries the live leaderboard, top score {rows[0]['score']}")

        # Finishing freezes the outcome for good.
        fast.clear()
        slow.clear()
        finished = await finish(competition_id, channel_layer)
        if finished.get("winner") is None:
            fail(f"finishing produced no winner: {finished}")
        results = await fast.wait_for("competition_results")
        if results.get("winner") != finished.get("winner"):
            fail("a screen was told a different winner than was published")
        ok(f"finishing published a final result to both labs: {results['winner']!r}")

        stored = await query(stored_result, competition_id)
        if stored is None or stored.winner_name != results["winner"]:
            fail(f"the final result was not persisted: {stored}")
        ok("the final result is persisted in the database")
    finally:
        await fast.close()
        await slow.close()
        clock_runner.cancel_all_clocks()

    print("\nEvery check passed against a real Daphne and a real Redis.")
    return 0


def main() -> int:
    # Minted here, not inside build_fixtures, so that the finally below can purge
    # even a build that raised half way through. A check that leaves fixtures
    # behind when it crashes is worse than one that never runs.
    tag = uuid.uuid4().hex[:6]
    fixtures = {}
    try:
        fixtures = in_thread(build_fixtures, tag)
        print(
            f"competition {fixtures['competition_id']}: two labs, one question, "
            f"key {CORRECT!r}",
            flush=True,
        )
        return asyncio.run(run_checks(fixtures))
    finally:
        # Everything this run created, not just the competition: accounts,
        # classrooms, screens and questions were all left behind before, because
        # nothing cascades to them. `check_fixtures` is shared with the other
        # networked checks so the purge cannot drift between them.
        # `competition_id` is None when the build never got that far; every
        # other lookup is still bounded by the tag, so a partial build is purged
        # as completely as a finished one.
        competition_id = fixtures.get("competition_id")
        discard = in_thread(
            discard_check_fixtures, tag=tag, competition_id=competition_id
        )
        residue = in_thread(
            assert_no_residue, tag=tag, competition_id=competition_id
        )
        print(f"[cleanup] removed {discard}", flush=True)
        if residue:
            print(f"[cleanup] WARNING: residue left behind: {residue}", flush=True)
        else:
            print("[cleanup] nothing left behind", flush=True)


if __name__ == "__main__":
    sys.exit(main())