"""The Phase H scenario, run against a real Daphne and a real Redis.

One teacher and three classrooms, all on separate WebSocket connections over real
TCP sockets, with no mock anywhere. The script drives the exact sequence from the
brief:

    1. Teacher starts competition
    2. All screens connect
    3. Teacher starts question
    4. All screens receive the same question
    5. Classroom A answers
    6. Classroom B answers
    7. Classroom C does not answer
    8. Teacher uses "End Question"
    9. All screens transition to closed
   10. Result is revealed
   11. Leaderboard updates
   12. Teacher clicks Next Question
   13. All screens move to the next question

and then repeats the question with an automatic timeout instead of the button, so
both triggers to ``ANSWERING_CLOSED`` are exercised against a live server.

Why the teacher needs a cookie
------------------------------
The teacher socket is authorised from the Django session, not from anything it
sends, so this client has to present a real session cookie - the same one a
browser would. It obtains one through Django's own test client rather than by
driving the login form, because the authentication being checked here is the
WebSocket's use of an already-issued session, not the login form itself. The
unauthorised cases (anonymous socket, another teacher's socket) are covered by
``live.tests.test_live_controls`` and ``live.tests.test_live_views``.

Run inside the running container::

    docker compose exec -T challenge-web python -m live.tests.live_multi_client_check

Exits non-zero on the first failed expectation and cleans up after itself.
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
from django.test import Client as DjangoTestClient  # noqa: E402
from django.utils import timezone  # noqa: E402

from core.tests.utils import (  # noqa: E402
    create_classroom,
    create_competition,
    create_screen,
    create_short_answer_question,
    create_teacher,
    create_true_false_question,
)
from live import clock_runner  # noqa: E402
from live.tests.check_fixtures import (  # noqa: E402
    assert_no_residue,
    discard_check_fixtures,
)
from live.tests.wsclient import WebSocketClient  # noqa: E402

HOST = os.environ.get("LIVE_CHECK_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIVE_CHECK_PORT", "8000"))

LABS = ("Science Lab A", "Science Lab B", "Science Lab C")

# Exactly what the classroom screen sends for a true/false question: the button
# labels. Sending something else - "Yes", say - is correctly refused, because
# `accepts` admits only "true"/"false" however they are cased.
ANSWER_PAIRS = ("True", "False")


def ok(message: str) -> None:
    print(f"  PASS  {message}", flush=True)


def fail(message: str) -> None:
    print(f"  FAIL  {message}", flush=True)
    raise SystemExit(1)


def in_thread(func, *args, **kwargs):
    """Run a synchronous call from outside any event loop."""
    return async_to_sync(database_sync_to_async(func))(*args, **kwargs)


async def query(func, *args, **kwargs):
    """The same, from inside the check's own event loop."""
    return await database_sync_to_async(func)(*args, **kwargs)


async def wait_for_where(client: WebSocketClient, event_type: str, predicate, what: str):
    """The first message of ``event_type`` satisfying ``predicate``.

    Needed wherever the same event type arrives more than once with different
    contents. Reading the *first* ``state_sync`` would report the number of
    screens connected before any had joined, and the first ``answer_progress``
    would count one answer when two had been sent - both would make the check
    assert something true of the wrong moment.
    """
    deadline = asyncio.get_event_loop().time() + 10.0
    while True:
        for message in client.messages:
            if message.get("type") == event_type and predicate(message):
                return message
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"{client.label}: no {event_type} with {what} within 10s. "
                f"Saw {[m.get('type') for m in client.messages]}."
            )
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def build_fixtures(tag: str) -> dict:
    """A three-lab round with two questions of different types.

Question 1 is true/false so that a wrong answer is unambiguous; question 2 is
    short answer, so the timeout run also exercises a question type that arrived
    in this phase.

    ``tag`` is minted by ``main`` and passed in, so the same value bounds both the
    fixtures created here and the cleanup afterwards. Every question's text
    carries it: cleanup finds its rows by matching on it, and a question whose
    text does not include the tag could never be matched and so would survive
    every run.
    """
    from core.tests.utils import TEST_PASSWORD

    teacher = create_teacher(username=f"multi.{tag}")
    classrooms = [create_classroom(name=f"{name} {tag}") for name in LABS]

    question_one = create_true_false_question(
        text=f"Water boils at 100 °C at sea level. {tag}", value=True
    )
    question_two = create_short_answer_question(
        text=f"Which gas do plants take in? {tag}",
        accepted_answers=["carbon dioxide", "co2"],
        explanation="Plants take in carbon dioxide for photosynthesis.",
    )

    competition = create_competition(
        teacher=teacher,
        title=f"Multi client check {tag}",
        classrooms=classrooms,
        questions=[question_one, question_two],
    )

    # A real session cookie, exactly as the login form would issue it.
    # `.value` is not optional: iterating the jar directly yields Morsel objects,
    # whose repr would be sent as the cookie and rejected as malformed.
    http = DjangoTestClient()
    logged_in = http.login(username=teacher.username, password=TEST_PASSWORD)
    if not logged_in:
        raise AssertionError("could not obtain a teacher session")

    return {
        # The run's unique tag, needed by the cleanup below to bound every
        # lookup so it can only ever remove rows this run created.
        "tag": tag,
        "competition_id": competition.pk,
        "cookies": {name: morsel.value for name, morsel in http.cookies.items()},
        "screens": [
            create_screen(
                name=f"{LABS[index]} board {tag}", classroom=classroom
            ).screen_id
            for index, classroom in enumerate(classrooms)
        ],
        "classroom_ids": [classroom.pk for classroom in classrooms],
    }


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


def teacher_client(fixtures: dict) -> WebSocketClient:
    return WebSocketClient(
        "teacher",
        f"/ws/live/teacher/{fixtures['competition_id']}/",
        HOST,
        PORT,
        cookies=fixtures["cookies"],
    )


def lab_client(label: str, screen_id: str) -> WebSocketClient:
    return WebSocketClient(label, f"/ws/live/screen/?screen_id={screen_id}", HOST, PORT)


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


async def run_checks(fixtures: dict) -> int:
    channel_layer = get_channel_layer()
    competition_id = fixtures["competition_id"]

    teacher = teacher_client(fixtures)
    labs = [
        lab_client(LABS[index], screen_id)
        for index, screen_id in enumerate(fixtures["screens"])
    ]

    await teacher.connect()
    try:
        # -- 1. the teacher starts the round ---------------------------------
        await teacher.wait_for("welcome")
        await teacher.send({"type": "start_competition"})
        await teacher.wait_for("competition_started")
        ok("teacher started the competition")

        # -- 2. all three labs connect --------------------------------------
        for lab in labs:
            await lab.connect()
        for lab in labs:
            await lab.wait_for("state_sync")
        ok(f"all {len(labs)} labs connected and received their state")

        presence = await wait_for_where(
            teacher,
            "presence",
            lambda m: m.get("connected_screens") == len(labs),
            f"connected_screens == {len(labs)}",
        )
        ok(
            f"the teacher sees {presence['connected_screens']} connected screens"
        )

        # -- 3. the teacher starts question 1 --------------------------------
        await teacher.send({"type": "start_question"})
        opened = [await lab.wait_for("question_started") for lab in labs]

        # -- 4. all three received the same question -------------------------
        texts = {message["text"] for message in opened}
        if len(texts) != 1:
            fail(f"the labs received different questions: {texts}")
        if {m["question_number"] for m in opened} != {1}:
            fail(f"unexpected question numbers: {[m['question_number'] for m in opened]}")
        ok(f"all three labs received question 1 identically: {texts.pop()!r}")

        for message in opened:
            if "correct_answer" in message or "explanation" in message:
                fail(f"the open question leaked the key: {message}")
        ok("the open question carried no answer key on any screen")

        # -- 5/6. labs A and B answer, C does not ---------------------------
        await labs[0].send({"type": "submit_answer", "answer": ANSWER_PAIRS[0]})
        await labs[0].wait_for("answer_accepted")
        await labs[1].send({"type": "submit_answer", "answer": ANSWER_PAIRS[1]})
        await labs[1].wait_for("answer_accepted")
        ok("labs A and B answered; lab C did not")

        progress = await wait_for_where(
            labs[2],
            "answer_progress",
            lambda m: m.get("answers_count") == 2,
            "answers_count == 2",
        )
        if progress["classes_count"] != 3:
            fail(f"lab C should have seen 3 classrooms taking part: {progress}")
        if "correct_answer" in progress:
            fail(f"progress leaked the key: {progress}")
        ok("lab C, which did not answer, saw only a count of 2")

        # -- 8. the teacher ends the question --------------------------------
        await teacher.send({"type": "end_question"})

        # -- 9. every screen transitions to closed --------------------------
        closed = [await lab.wait_for("answering_closed") for lab in labs]
        for message in closed:
            if message["state"] != "answering_closed":
                fail(f"a screen did not transition to closed: {message}")
        ok("all three screens transitioned to closed after End Question")

        # -- 10. the result is revealed everywhere ---------------------------
        reveals = [await lab.wait_for("result_revealed") for lab in labs]
        keys = {reveal["correct_answer"] for reveal in reveals}
        if keys != {"true"}:
            fail(f"the labs received different answer keys: {keys}")
        ok("all three screens received the same answer key at the reveal")

        for reveal in reveals:
            if reveal["answers_count"] != 2:
                fail(f"the reveal miscounts the answers: {reveal['answers_count']}")
        correct_rows = [row for row in reveals[0]["answers"] if row["is_correct"]]
        if len(correct_rows) != 1:
            fail(f"exactly one lab should be correct: {reveals[0]['answers']}")
        ok("the reveal names the one correct lab and omits the lab that sat it out")

        # -- 11. the leaderboard updates -------------------------------------
        rows = reveals[0]["leaderboard"]["rows"]
        if len(rows) != 3:
            fail(f"the leaderboard should hold all three labs: {rows}")
        top = rows[0]
        if top["score"] <= 0 or top["correct_answers"] != 1:
            fail(f"the leaderboard did not score the round: {rows}")
        trailing = [row for row in rows if row["answers_given"] == 0]
        if len(trailing) != 1:
            fail(f"the lab that did not answer should be on the board at zero: {rows}")
        ok(f"leaderboard updated across three labs, top score {top['score']}")

        # -- 12/13. the teacher moves on ------------------------------------
        for lab in labs:
            lab.clear()
        teacher.clear()
        await teacher.send({"type": "advance"})

        # Wait on the teacher first. If the advance is refused, the labs go quiet
        # and the only place the reason exists is the teacher's socket - so
        # failing here names the cause instead of reporting a timeout on a screen
        # that was told nothing.
        outcome = await teacher.wait_for_any(("question_started", "error"))
        if outcome["type"] == "error":
            fail(f"the teacher could not advance: {outcome}")

        second = [await lab.wait_for("question_started") for lab in labs]

        if {m["question_number"] for m in second} != {2}:
            fail(f"the labs did not all move to question 2: {second}")
        if {m["question_type"] for m in second} != {"short_answer"}:
            fail(f"question 2 should be a short answer: {second}")
        ok("all three screens moved on to question 2 (short answer)")

        # -- the same again, this time by the clock --------------------------
        await run_timeout_pass(teacher, labs, channel_layer, competition_id)
    finally:
        for lab in labs:
            await lab.close()
        await teacher.close()
        clock_runner.cancel_all_clocks()

    print(
        "\nTeacher plus three classrooms passed every step, over real sockets, "
        "against a real Daphne and Redis."
    )
    return 0


async def run_timeout_pass(teacher, labs, channel_layer, competition_id) -> None:
    """Question 2, ended by the server clock rather than by the button.

    Same destination, different trigger. The room must be unable to tell which
    happened, so the payload is asserted to be indistinguishable - only the log
    distinguishes them.
    """
    await labs[0].send({"type": "submit_answer", "answer": "carbon dioxide"})
    await labs[0].wait_for("answer_accepted")
    await labs[2].send({"type": "submit_answer", "answer": "  Oxygen  "})
    await labs[2].wait_for("answer_accepted")
    ok("labs A and C answered question 2; lab B did not")

    # Arm the clock with a deadline already reached, which is what the background
    # clock does the moment a question's duration elapses.
    await clock_runner.start_clock(
        competition_id, channel_layer, timezone.now() - timedelta(seconds=1)
    )

    closed = [await lab.wait_for("answering_closed") for lab in labs]
    for message in closed:
        if message["state"] != "answering_closed":
            fail(f"the clock did not close a lab's answering period: {message}")
    ok("the server clock closed answering on all three screens")

    reveals = [await lab.wait_for("result_revealed") for lab in labs]
    keys = {reveal["correct_answer"] for reveal in reveals}
    if keys != {"carbon dioxide"}:
        fail(f"the timeout reveal disagreed about the key: {keys}")
    if reveals[0]["answers_count"] != 2:
        fail(f"the timeout reveal miscounts the answers: {reveals[0]['answers_count']}")
    ok("the timeout revealed the same key to every screen")

    # Lab C typed "  Oxygen  ": padded and differently cased, and not the answer.
    # This is the normalised comparison under test, on a live socket.
    reported = [
        row for row in reveals[0]["answers"] if "Oxygen" in row["selected_answer"]
    ]
    if not reported:
        fail(f"lab C's answer was not reported back: {reveals[0]['answers']}")
    if reported[0]["is_correct"]:
        fail("a padded, differently-cased wrong answer was scored correct")
    ok("the padded wrong answer was scored wrong, and shown back as it was sent")

    correct = [row for row in reveals[0]["answers"] if row["is_correct"]]
    if len(correct) != 1 or "carbon" not in correct[0]["selected_answer"]:
        fail(f"the right answer was not scored as correct: {reveals[0]['answers']}")
    ok("lab A's correct short answer was scored correct")

    explanation = {reveal.get("explanation") for reveal in reveals}
    if explanation != {"Plants take in carbon dioxide for photosynthesis."}:
        fail(f"the explanation was not carried on every reveal: {explanation}")
    ok("the explanation reached every screen at the reveal")


def main() -> int:
    # Minted here, not inside build_fixtures, so the finally below can purge even a
    # build that raised half way through. A check that leaves fixtures behind when
    # it crashes is worse than one that never runs.
    tag = uuid.uuid4().hex[:6]
    fixtures = {}
    try:
        fixtures = in_thread(build_fixtures, tag)
        print(
            f"competition {fixtures['competition_id']}: 1 teacher and "
            f"{len(fixtures['screens'])} classrooms, two questions",
            flush=True,
        )
        return asyncio.run(run_checks(fixtures))
    finally:
        # Everything this run created, not just the competition. Deleting only
        # the competition used to leave the teacher account, the three
        # classrooms, the three screens and both questions behind, because
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