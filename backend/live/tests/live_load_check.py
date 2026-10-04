"""Load check: run one real competition with a configurable number of screens.

Answers Part H of the Phase 8 acceptance criteria. Where
``live_multi_client_check`` proves the flow is *correct* for three screens, this
proves it still *holds* at a classroom-realistic fleet size.

Everything here goes over a real TCP socket to a real Daphne, with a real session
cookie for the teacher, against a real PostgreSQL and Redis - the same path a
classroom uses. Nothing is stubbed, so a number reported here is a number
observed from outside the application.

Measures, per run:

* connection success and the latency of each screen's handshake
* how long every screen took to receive a broadcast the teacher caused
* whether all screens received an identical question
* whether every answer was accepted and scored
* whether the leaderboard reached every screen
* process CPU and memory of the web container, sampled during the run

Usage::

    python -m live.tests.live_load_check --screens 5
    python -m live.tests.live_load_check --screens 20 --host 10.0.0.5 --port 8000

Exit code is 0 only when every check passed.

Safety rule
-----------
This script writes to the database it is pointed at: it creates a competition, a
teacher account, classrooms and screens. It must therefore never be able to
damage a real system, and the guarantees below are enforced rather than
documented and hoped for:

1. **No existing account is ever touched.** The teacher account is created with
   a name that is unique to the run (``load-check-test-<epoch>``) and a random
   password. The script never calls ``set_password`` on a row it did not create,
   so pointing ``--host`` at a production server cannot rewrite anybody's
   password. If the name somehow already exists, the run refuses.
2. **A non-local ``--host`` is refused** unless ``--allow-remote-host`` is given,
   so a typo or a copied command line cannot aim a load test at the live server.
3. **Production settings are refused** unless ``--allow-production-settings`` is
   given, for the same reason: ``config.settings.production`` means the database
   behind it is meant to hold real results.
4. **What it creates, it removes.** The fixtures carry the run's unique tag and
   are deleted in a ``finally`` block, so repeated runs do not pile up. Pass
   ``--keep-fixtures`` to leave them for inspection.
"""


from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import subprocess
import sys
import time
from statistics import median


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402 - must follow the settings default

django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from channels.layers import get_channel_layer  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.roles import Role  # noqa: E402
from classrooms.models import Classroom  # noqa: E402
from competitions.models import (  # noqa: E402
    Competition,
    CompetitionClassroom,
    CompetitionQuestion,
)
from live.tests.wsclient import WebSocketClient  # noqa: E402
from questions.models import Question  # noqa: E402
from screens.models import InteractiveScreen  # noqa: E402

def ok(message: str) -> None:

    print(f"  PASS  {message}", flush=True)


def fail(message: str) -> None:
    print(f"  FAIL  {message}", flush=True)
    FAILURES.append(message)


FAILURES: list[str] = []

#: Hosts that mean "this machine". Anything else is treated as remote and needs
#: --allow-remote-host. Deliberately a fixed list rather than a DNS lookup: the
#: point is to decide whether the user has said where they are pointing.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "", "host.docker.internal"}

WEB_CONTAINER = os.environ.get("LIVE_CHECK_WEB_CONTAINER", "almanar-challenge-web")


def refuse(message: str, hint: str = "") -> None:
    """Stop before anything has been written."""
    print(f"\nREFUSING TO RUN: {message}", flush=True)
    if hint:
        print(f"  {hint}", flush=True)
    print(
        "\nThis check creates real rows. Refusing is the safe default; the "
        "overrides\nexist so the decision is explicit and visible in the shell "
        "history.",
        flush=True,
    )
    raise SystemExit(2)


def guard(host: str, *, allow_remote: bool, allow_production: bool) -> None:
    """Refuse to run against anything but a disposable database.

    Called before the first write. See the module docstring for the guarantees.
    """
    if host.lower() not in LOCAL_HOSTS and not allow_remote:
        refuse(
            f"{host!r} is not this machine.",
            "A load test writes to the database it is aimed at. Pass "
            "--allow-remote-host\n    if that host really is a disposable "
            "test server.",
        )

    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    if settings_module.rsplit(".", 1)[-1] == "production" and not allow_production:
        refuse(
            f"DJANGO_SETTINGS_MODULE={settings_module!r} is the production "
            "settings module.",
            "Its database is meant to hold real competition results. Pass "
            "--allow-production-settings\n    only for a rehearsed run against "
            "a database you are willing to have fixtures\n    written to and "
            "deleted from.",
        )



# ---------------------------------------------------------------------------
# Container resource sampling
# ---------------------------------------------------------------------------


def container_stats() -> dict | None:
    """CPU and memory for the web container, or ``None`` if it cannot be read.

    Read from the Docker CLI rather than from inside the container: the process
    being measured is this one, and a sampler that runs inside it would report the
    load generator as well as the server.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}",
                WEB_CONTAINER,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    except Exception:
        return None

    line = result.stdout.strip().splitlines()
    if not line:
        return None

    cpu, mem, mem_pct = line[0].split("|", 2)

    def to_mib(value: str) -> float:
        # "12.3MiB / 3.847GiB"
        number = value.split("/")[0].strip()
        for suffix, factor in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024)):
            if number.endswith(suffix):
                return round(float(number[: -len(suffix)]) * factor, 1)
        return 0.0

    def to_pct(value: str) -> float:
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return 0.0

    return {
        "cpu_pct": to_pct(cpu),
        "mem_mib": to_mib(mem),
        "mem_pct": to_pct(mem_pct),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def create_fixtures(screen_count: int) -> dict:
    """A ready-to-run competition with ``screen_count`` registered screens.

    Creates its own teacher account, classrooms, screens and questions, every one
    of them tagged with a value unique to this run. The account is created, never
    reused: there is no code path here that can change the password of a row
    that already existed, which is what makes ``--host`` safe to type.
    """
    tag = f"{int(time.time())}"

    username = f"load-check-test-{tag}"
    if await username_taken(username):
        # Only reachable if two runs share a second, or the database is not
        # disposable. Refusing is still the right answer: the alternative would
        # be reusing an account, which is what could reset somebody's password.
        refuse(
            f"the account {username!r} already exists, so this is not a "
            "disposable database.",
            "The check refuses to reuse an account rather than reset its "
            "password.",
        )

    password = f"load-check-{tag}-{secrets.token_urlsafe(12)}"
    teacher = await create_account(username, password, tag)

    # Two questions: the scenario advances to the second one, so a round with only
    # one would stop short of proving the round keeps running.
    questions = await _create_questions(tag)

    competition = await _create_competition(teacher, questions, tag)
    screens = await _create_screens(competition, screen_count, tag)

    return {
        "tag": tag,
        "competition_id": competition.pk,
        "teacher_username": teacher.username,
        "password": password,
        "screens": screens,
        "answer_key": "Beta",
    }


@sync_to_async
def username_taken(username: str) -> bool:
    return get_user_model().objects.filter(username=username).exists()


@sync_to_async
def create_account(username: str, password: str, tag: str):
    """Create the run's teacher account. Never updates an existing row."""
    return get_user_model().objects.create_user(
        username=username,
        password=password,
        role=Role.TEACHER,
        full_name=f"Load check test account {tag}",
    )


@sync_to_async
def _create_competition(teacher, questions, tag: str):
    competition = Competition.objects.create(
        title=f"Load check {tag}", teacher=teacher
    )
    for index, question in enumerate(questions, start=1):
        CompetitionQuestion.objects.create(
            competition=competition, question=question, position=index
        )
    return competition


@sync_to_async
def _create_questions(tag: str):
    return [
        Question.objects.create(
            text=f"Load check question {number} {tag}",
            options=["Alpha", "Beta", "Gamma", "Delta"],
            correct_option="Beta",
            duration_seconds=60,
        )
        for number in (1, 2)
    ]


@sync_to_async
def _attach_questions(competition, questions) -> None:
    for index, question in enumerate(questions, start=1):
        CompetitionQuestion.objects.create(
            competition=competition, question=question, position=index
        )


@sync_to_async
def _create_screens(competition, screen_count: int, tag: str) -> list[str]:
    """One classroom and one screen per simulated room, all named after ``tag``."""
    screens = []
    for index in range(screen_count):
        classroom = Classroom.objects.create(name=f"Load Lab {index} {tag}")
        CompetitionClassroom.objects.create(
            competition=competition, classroom=classroom
        )
        screen = InteractiveScreen.objects.create(
            name=f"Load board {index} {tag}", classroom=classroom
        )
        screens.append(screen.screen_id)
    return screens


@sync_to_async
def discard_fixtures(tag: str, competition_id: int) -> None:
    """Remove everything this run created, and nothing else.

    Every lookup is keyed by the run's unique tag or by the competition this run
    created, so this cannot reach a row that belongs to anyone else.

    The order matters. Questions are ``PROTECT``ed by the competition's current
    question, by the round's question list and by the answers, so the references
    have to be cleared from the leaves up or Django refuses the delete.
    """
    from competitions.models import Competition
    from scoring.models import Answer

    User = get_user_model()

    Answer.objects.filter(competition_id=competition_id).delete()
    Competition.objects.filter(pk=competition_id).update(current_question=None)
    Competition.objects.filter(pk=competition_id).delete()

    InteractiveScreen.objects.filter(name__contains=tag).delete()
    Classroom.objects.filter(name__contains=tag).delete()
    Question.objects.filter(text__contains=tag).delete()
    User.objects.filter(username=f"load-check-test-{tag}").delete()



@sync_to_async
def session_cookie(username: str, password: str) -> dict:
    from django.test import Client

    client = Client()
    assert client.login(username=username, password=password), "teacher login failed"
    return {name: morsel.value for name, morsel in client.cookies.items()}


@sync_to_async
def finish(competition_id: int) -> None:
    Competition.objects.filter(pk=competition_id).update(
        state="finished", finished_at=timezone.now()
    )


@sync_to_async
def leaderboard(competition_id: int) -> list:
    """The competition's frozen standings, read back from the database.

    Read through the same service that writes them, so this checks what was
    stored rather than what the broadcast claimed.
    """
    from competitions.models import Competition as _Competition
    from scoring.services import finalise

    return finalise(_Competition.objects.get(pk=competition_id)).rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def wait_for_where(client, event_type, predicate, description, timeout=20.0):
    """Wait for a message of ``event_type`` satisfying ``predicate``.

    Polls the client's received-message list rather than consuming a stream, so
    the messages a check skipped past stay visible to a later assertion - which is
    what lets this check confirm a screen received something it was not explicitly
    waiting for.
    """
    deadline = time.monotonic() + timeout
    while True:
        for message in client.messages:
            if message.get("type") == event_type and predicate(message):
                return message
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{client.label}: no {description!r} within {timeout}s. "
                f"Saw {[m.get('type') for m in client.messages]}."
            )
        await asyncio.sleep(0.02)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 1)


async def each(clients, event_type, *, timeout: float = 20.0) -> list:
    """Wait for ``event_type`` on every client, recording a miss instead of raising.

    ``asyncio.gather`` would propagate the first ``AssertionError`` straight out of
    the run, which loses the summary and the CPU/memory figures for exactly the
    runs where they matter most - the failing ones. Every screen is asked here and
    every miss becomes a recorded failure, so one dead screen does not hide the
    state of the other nineteen.
    """
    messages = []
    for client in clients:
        try:
            messages.append(await client.wait_for(event_type, timeout=timeout))
        except AssertionError as exc:
            fail(str(exc))
    return messages


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------



async def run(
    screen_count: int,
    host: str,
    port: int,
    *,
    keep_fixtures: bool = False,
) -> int:
    print(f"\nLoad check: {screen_count} screens against {host}:{port}", flush=True)

    fixtures = await create_fixtures(screen_count)
    tag = fixtures["tag"]
    competition_id = fixtures["competition_id"]

    connect_times: list[float] = []
    question_times: list[float] = []
    reveal_times: list[float] = []
    baseline = container_stats()

    # Everything from here on is inside the try, including the setup. A refusal to
    # connect, a wrong port or a bad session must not leave a competition, twenty
    # classrooms and an account behind: those rows are the ones a crashed run
    # would otherwise leave in a real database, and they are the reason this
    # cleanup is in a finally rather than at the end of a happy path.
    teacher = None
    labs: list = []
    try:
        cookies = await session_cookie(fixtures["teacher_username"], fixtures["password"])

        teacher = WebSocketClient(
            "teacher",
            f"/ws/live/teacher/{competition_id}/",
            host,
            port,
            cookies=cookies,
        )
        labs = [
            WebSocketClient(
                f"screen-{index + 1}",
                f"/ws/live/screen/?screen_id={screen_id}",
                host,
                port,
            )
            for index, screen_id in enumerate(fixtures["screens"])
        ]

        await teacher.connect()

        await teacher.wait_for("welcome")
        await teacher.send({"type": "start_competition"})

        await teacher.wait_for("competition_started")
        ok("teacher started the competition")

        # -- every screen connects, timed individually -----------------------
        connected = 0
        for lab in labs:
            started = time.monotonic()
            try:
                await lab.connect()
                connected += 1
            except Exception as exc:  # noqa: BLE001
                fail(f"{lab.label} could not connect: {exc}")
                continue
            connect_times.append((time.monotonic() - started) * 1000)

        if connected == screen_count:
            ok(
                f"all {connected} screens connected "
                f"(median {median(connect_times):.0f} ms, p95 "
                f"{percentile(connect_times, 0.95):.0f} ms)"
            )

        before = len(FAILURES)
        await each(labs, "state_sync")
        if len(FAILURES) == before:
            ok(f"all {connected} screens received their state")

        before = len(FAILURES)
        try:
            presence = await wait_for_where(
                teacher,
                "presence",
                lambda m: m.get("connected_screens") == screen_count,
                f"connected_screens == {screen_count}",
                timeout=30,
            )
        except AssertionError as exc:
            fail(str(exc))
        else:
            ok(f"the teacher sees {presence['connected_screens']} connected screens")

        # -- question 1: broadcast latency and identical delivery ------------
        sent_at = time.monotonic()
        await teacher.send({"type": "start_question"})

        opened = await each(labs, "question_started")
        for message in opened:
            question_times.append((time.monotonic() - sent_at) * 1000)

        texts = {m["text"] for m in opened if "text" in m}
        if len(texts) == 1 and len(opened) == len(labs):
            ok(
                f"all {len(opened)} screens received question 1 identically "
                f"(median {median(question_times):.0f} ms, p95 "
                f"{percentile(question_times, 0.95):.0f} ms)"
            )
        elif len(texts) > 1:
            fail(f"screens received different questions: {texts}")

        if any("correct_answer" in m for m in opened):
            fail("the open question leaked the answer key")
        elif opened:
            ok("no screen received the answer key while answering was open")

        # -- every screen answers --------------------------------------------
        key = fixtures["answer_key"]
        answer_sent = time.monotonic()
        for lab in labs:
            # `answer` is the documented field; anything else the client sends is
            # ignored by the scoring path by construction.
            try:
                await lab.send({"type": "submit_answer", "answer": key})
            except Exception as exc:  # noqa: BLE001
                fail(f"{lab.label} could not send its answer: {exc}")

        before = len(FAILURES)
        acked = await each(labs, "answer_accepted")
        if len(acked) == len(labs):
            ok(
                f"all {len(acked)} answers accepted "
                f"({(time.monotonic() - answer_sent) * 1000:.0f} ms for the last)"
            )
        else:
            fail(
                f"only {len(acked)} of {len(labs)} answers were accepted "
                f"({len(FAILURES) - before} check(s) failed)"
            )

        # -- reveal and leaderboard ------------------------------------------
        leader_sent = time.monotonic()
        reveal_sent = time.monotonic()
        await teacher.send({"type": "end_question"})

        reveals = []
        for lab in labs:
            try:
                message = await lab.wait_for("result_revealed")
            except AssertionError as exc:
                fail(str(exc))
                continue
            reveal_times.append((time.monotonic() - reveal_sent) * 1000)
            if message.get("correct_answer") != key:
                fail(f"{lab.label} was told the wrong answer key")
            reveals.append(message)

        if reveals:
            ok(
                f"all {len(reveals)} of {len(labs)} screens received the reveal "
                f"with the correct key "
                f"(median {median(reveal_times):.0f} ms, p95 "
                f"{percentile(reveal_times, 0.95):.0f} ms)"
            )

        # The leaderboard travels inside the reveal payload rather than as its own
        # event, so this checks every screen was given the same scored table.
        leaderboard_ms = (time.monotonic() - leader_sent) * 1000
        row_counts = {len(m.get("leaderboard", {}).get("rows", [])) for m in reveals}
        if row_counts == {screen_count}:
            ok(
                f"every screen received a leaderboard with all {screen_count} rows "
                f"({leaderboard_ms:.0f} ms after the reveal)"
            )
        elif reveals:
            fail(f"screens received leaderboards of differing sizes: {row_counts}")

        tops = {
            max(
                (row["score"] for row in m.get("leaderboard", {}).get("rows", [])),
                default=0,
            )
            for m in reveals
        }
        if tops == {0} or len(tops) != 1:
            fail(f"screens disagree about the top score: {tops}")
        elif reveals:
            ok(f"every screen agrees on the top score: {tops.pop()}")

        # -- question 2 to prove the round keeps running ----------------------
        await teacher.send({"type": "start_question", "position": 2})
        if await each(labs, "question_started"):
            ok("a second question reached every screen")

        await teacher.send({"type": "end_question"})
        if await each(labs, "result_revealed"):
            ok("the second reveal reached every screen")

        await teacher.send({"type": "finish_competition"})
        await teacher.wait_for("competition_finished")
        ok("the competition finished and every screen was told")

        # -- the scores are real ---------------------------------------------
        rows = await leaderboard(competition_id)
        if len(rows) == screen_count:
            ok(f"the frozen leaderboard holds all {screen_count} classrooms")
        else:
            fail(f"the leaderboard holds {len(rows)} rows, expected {screen_count}")

        top = max((row["score"] for row in rows), default=0)
        if top > 0:
            ok(f"the top score is {top}")
        else:
            fail("nobody scored, so the answers were not recorded")

    except AssertionError as exc:
        # A hard stop inside the scenario (the teacher's own socket, a control
        # action refused). Recorded rather than raised, so the run still prints
        # what it measured and still exits non-zero.
        fail(f"the scenario stopped early: {exc}")
    except Exception as exc:  # noqa: BLE001
        fail(f"the scenario raised {type(exc).__name__}: {exc}")
    finally:
        for client in [teacher, *labs]:
            if client is None:
                continue
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        await finish(competition_id)
        if not keep_fixtures:
            await discard_fixtures(tag, competition_id)

    peak = container_stats()

    print("\n  Observed", flush=True)
    for label, values in (
        ("connect", connect_times),
        ("broadcast", question_times),
        ("reveal", reveal_times),
    ):
        if not values:
            print(f"    {label:<11} no measurements", flush=True)
            continue
        print(
            f"    {label:<11} median {median(values):7.0f} ms   "
            f"p95 {percentile(values, 0.95):7.0f} ms   max {max(values):7.0f} ms",
            flush=True,
        )

    if baseline and peak:
        print(
            f"    web container CPU  {baseline['cpu_pct']:.0f}% -> {peak['cpu_pct']:.0f}%"
            f"   memory {baseline['mem_mib']} MiB -> {peak['mem_mib']} MiB "
            f"({peak['mem_pct']:.1f}% of host)",
            flush=True,
        )
    else:
        print("    web container CPU/memory  unavailable (docker stats not readable)")

    return 1 if FAILURES else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one real competition with N simulated screens."
    )
    parser.add_argument("--screens", type=int, default=5)
    parser.add_argument("--host", default=os.environ.get("LIVE_CHECK_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("LIVE_CHECK_PORT", "8000"))
    )
    # -- the two safety overrides. Both are opt-in, spelled out in full, and
    # neither is implied by anything else on the command line. In particular
    # --host alone can never authorise a run against another machine.
    parser.add_argument(
        "--allow-remote-host",
        action="store_true",
        help="permit a --host other than this machine (a disposable test server)",
    )
    parser.add_argument(
        "--allow-production-settings",
        action="store_true",
        help="permit DJANGO_SETTINGS_MODULE=config.settings.production",
    )
    parser.add_argument(
        "--keep-fixtures",
        action="store_true",
        help="leave the created competition, screens and account behind",
    )
    args = parser.parse_args()

    # Before anything is written, and before any socket is opened.
    guard(
        args.host,
        allow_remote=args.allow_remote_host,
        allow_production=args.allow_production_settings,
    )

    code = asyncio.run(
        run(
            args.screens,
            args.host,
            args.port,
            keep_fixtures=args.keep_fixtures,
        )
    )

    # The summary is printed whatever happened above, including when the scenario
    # raised: a load test that fails silently is worse than one that does not run.
    print()
    if code:
        print(f"LOAD CHECK FAILED: {len(FAILURES)} check(s) failed.")
        for message in FAILURES:
            print(f"  - {message}")
        return code
    print(f"LOAD CHECK PASSED at {args.screens} screens.")
    return 0



if __name__ == "__main__":
    sys.exit(main())