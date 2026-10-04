"""Automated coverage for the live competition engine.

These tests speak the real WebSocket protocol through the real routing table
(``WebsocketCommunicator``), so they exercise routing, authentication,
authorisation and group broadcast together rather than in isolation.

The nine behaviours the phase requires are covered as follows:

===========================  ===========================================
Required behaviour            Test class
===========================  ===========================================
WebSocket connection          ScreenConnectionTests, TeacherConnectionTests
Invalid Screen ID             ScreenConnectionTests
Valid Screen ID               ScreenConnectionTests
Competition start             CompetitionStartTests
Question broadcast            QuestionBroadcastTests
Multiple screens, same event  MultipleScreenBroadcastTests
Timer state                   TimerStateTests
Reconnection                  ReconnectionTests
Unauthorised classroom        UnauthorizedClassroomTests
===========================  ===========================================
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels_redis.core import RedisChannelLayer
from channels.testing import WebsocketCommunicator
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from competitions.models import Competition, CompetitionState
from config.asgi import application
from core.tests.utils import (
    add_question,
    create_administrator,
    create_classroom,
    create_competition,
    create_question,
    create_screen,
    create_teacher,
    include_classroom,
)
from live import clock_runner, groups, heartbeat, presence, services
from live.events import (
    ANSWERING_CLOSED,
    COMPETITION_FINISHED,
    COMPETITION_STARTED,
    NEXT_QUESTION,
    QUESTION_STARTED,
    REJECTED,
    RESULT_REVEALED,
    STATE_SYNC,
    TICK,
    WELCOME,
)
from live.tests.support import (
    CONNECT_TIMEOUT_SECONDS,
    build_anonymous_teacher_scope,
    build_teacher_scope,
    drain_until,
    encode,
    origin_headers,
    receive_within,
    screen_url,
    send_message,
    teacher_url,
)


def _clear_channel_layer_cache() -> None:
    """Drop the memoised channel layer so an override actually takes effect.

    ``channels.layers`` memoises the backend, and exposes the reset used by its
    own ``setting_changed`` receiver. Calling it with ``enter=True`` rebuilds the
    layer from the current settings.
    """
    from channels.layers import channel_layers

    channel_layers._reset_backends(setting=None, enter=True)


class LiveTestCase(TransactionTestCase):
    """Base class providing a started competition and connected screens.

    ``TransactionTestCase`` rather than ``TestCase``: the consumers run real
    channel connections and background clock tasks, so the test needs genuine
    transaction boundaries and no wrapping transaction.

    The channel layer is replaced with the in-memory implementation
    deliberately. ``channels_redis`` pools Redis connections that are bound to
    the event loop which created them, and Django gives every async test a fresh
    event loop - so a shared Redis layer deadlocks with ``Timeout reading from
    Redis`` on the second async test. Group broadcast semantics are identical;
    Redis itself is exercised for real by ``core.tests.test_redis`` and by
    :class:`RedisChannelLayerLiveTests` below.
    """

    reset_sequences = True

    @classmethod
    def setUpClass(cls) -> None:
        # `channels.layers` memoises the layer instance in a module-level dict,
        # so changing CHANNEL_LAYERS alone would leave the previous (Redis)
        # instance in place. The cache is cleared around the override.
        cls._channel_layer_override = override_settings(
            CHANNEL_LAYERS={
                "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
            }
        )
        cls._channel_layer_override.enable()
        _clear_channel_layer_cache()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        cls._channel_layer_override.disable()
        _clear_channel_layer_cache()

    def setUp(self) -> None:
        super().setUp()
        presence.clear_members()
        clock_runner.cancel_all_clocks()
        self.addCleanup(presence.clear_members)
        self.addCleanup(clock_runner.cancel_all_clocks)
        # The consumers start the keepalive on connect; it must not outlive the
        # test that started it.
        heartbeat.stop()
        self.addCleanup(heartbeat.stop)
        # Django's TransactionTestCase has no addAsyncCleanup, so open sockets
        # are tracked here and closed by _run in the same event loop.
        self._open_communicators: list = []

    def _run(self, impl) -> None:
        """Run an async test body, then close its sockets in that same loop.

        A communicator belongs to the event loop that created it, so closing one
        from a *different* loop (which is what a plain `async_to_sync` call in
        tearDown would do) raises CancelledError. Doing the cleanup inside the
        body's own loop avoids that, and guarantees sockets are closed even when
        the test fails.
        """

        async def runner():
            try:
                await impl()
            finally:
                for communicator in self._open_communicators:
                    try:
                        await communicator.disconnect()
                    except Exception:  # noqa: BLE001 - must not mask the result
                        pass
                self._open_communicators.clear()

        async_to_sync(runner)()

    async def _connect_screen(self, screen):
        communicator = self._screen_communicator(screen.screen_id)
        # Generous timeout: connect() performs several database queries before
        # it can accept or refuse.
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)
        self._open_communicators.append(communicator)
        return communicator, connected

    def _screen_communicator(self, screen_id: str) -> WebsocketCommunicator:
        """A screen communicator that has not been connected yet.

        A screen browser has no session cookie; ``AuthMiddleware`` still
        requires the key, so an empty session is supplied - which is exactly
        what a screen genuinely presents. The screen authenticates with its
        Screen ID instead.
        """
        communicator = WebsocketCommunicator(
            application, screen_url(screen_id), headers=origin_headers()
        )
        return communicator

    async def _disconnect(self, communicator) -> None:
        await communicator.disconnect()
        if communicator in self._open_communicators:
            self._open_communicators.remove(communicator)

    async def _connect_teacher(self, user, competition):
        communicator = WebsocketCommunicator(
            application,
            teacher_url(competition.pk),
            headers=origin_headers(),
        )
        communicator.scope = await build_teacher_scope(user, competition.pk)
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)
        self._open_communicators.append(communicator)
        return communicator, connected

    async def _skip_intro(self, communicator):
        """Consume ``welcome`` and ``state_sync`` sent on connect."""
        await drain_until(communicator, STATE_SYNC)

    # -- async-safe fixture helpers ---------------------------------------
    #
    # An `async def test` runs inside the event loop, where Django refuses
    # direct database access. Every write inside such a test therefore goes
    # through these helpers, which run the synchronous factory or service call
    # in a worker thread. `setUp` is still synchronous and needs none of this.

    @staticmethod
    async def _in_thread(func, *args, **kwargs):
        return await database_sync_to_async(func)(*args, **kwargs)

    async def make_screen(self, *args, **kwargs):
        return await self._in_thread(create_screen, *args, **kwargs)

    async def make_competition(self, *args, **kwargs):
        return await self._in_thread(create_competition, *args, **kwargs)

    async def make_teacher(self, *args, **kwargs):
        return await self._in_thread(create_teacher, *args, **kwargs)

    async def make_question(self, *args, **kwargs):
        return await self._in_thread(create_question, *args, **kwargs)

    async def add_question(self, *args, **kwargs):
        return await self._in_thread(add_question, *args, **kwargs)

    async def start_competition(self, competition):
        return await self._in_thread(services.start_competition, competition)

    async def start_question(self, competition, position=None):
        return await self._in_thread(
            services.start_question, competition, position=position
        )

    async def refresh(self, instance):
        """Re-read an instance from the database, safely from an async body."""
        return await self._in_thread(instance.refresh_from_db)


# ---------------------------------------------------------------------------
# WebSocket connection, valid and invalid Screen ID
# ---------------------------------------------------------------------------


class ScreenConnectionTests(LiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.classroom = create_classroom(name="Science Lab 1")
        self.competition = create_competition(
            teacher=self.teacher, classrooms=[self.classroom]
        )
        services.start_competition(self.competition)

    def test_a_valid_screen_id_connects_and_is_welcomed(self) -> None:

        self._run(self._test_a_valid_screen_id_connects_and_is_welcomed_impl)


    async def _test_a_valid_screen_id_connects_and_is_welcomed_impl(self) -> None:
        screen = await self.make_screen(name="Front board", classroom=self.classroom)

        communicator, connected = await self._connect_screen(screen)

        self.assertTrue(connected)
        message = await receive_within(communicator)
        self.assertEqual(message["type"], WELCOME)
        self.assertEqual(message["screen"]["screen_id"], screen.screen_id)
        self.assertEqual(message["screen"]["classroom"], self.classroom.display_name)

    def test_a_valid_screen_id_receives_the_current_state(self) -> None:

        self._run(self._test_a_valid_screen_id_receives_the_current_state_impl)


    async def _test_a_valid_screen_id_receives_the_current_state_impl(self) -> None:
        screen = await self.make_screen(name="Front board", classroom=self.classroom)

        communicator, _ = await self._connect_screen(screen)

        message, _ = await drain_until(communicator, STATE_SYNC)
        self.assertEqual(message["competition_id"], self.competition.pk)
        self.assertEqual(message["state"], CompetitionState.WAITING)

    def test_an_invalid_screen_id_is_refused(self) -> None:

        self._run(self._test_an_invalid_screen_id_is_refused_impl)


    async def _test_an_invalid_screen_id_is_refused_impl(self) -> None:
        communicator = WebsocketCommunicator(
            application, screen_url("AM-ZZZZZZ"), headers=origin_headers()
        )

        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)

        self.assertFalse(connected)

    def test_a_missing_screen_id_is_refused(self) -> None:

        self._run(self._test_a_missing_screen_id_is_refused_impl)


    async def _test_a_missing_screen_id_is_refused_impl(self) -> None:
        communicator = WebsocketCommunicator(
            application, "/ws/live/screen/", headers=origin_headers()
        )

        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)

        self.assertFalse(connected)

    def test_an_inactive_screen_is_refused_like_an_unknown_one(self) -> None:

        self._run(self._test_an_inactive_screen_is_refused_like_an_unknown_one_impl)


    async def _test_an_inactive_screen_is_refused_like_an_unknown_one_impl(self) -> None:
        """Retired screens must not be distinguishable from unknown ones."""
        screen = await self.make_screen(
            name="Retired board", classroom=self.classroom, active=False
        )

        communicator = WebsocketCommunicator(
            application, screen_url(screen.screen_id), headers=origin_headers()
        )
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)

        self.assertFalse(connected)

    def test_a_screen_id_is_matched_case_insensitively(self) -> None:

        self._run(self._test_a_screen_id_is_matched_case_insensitively_impl)


    async def _test_a_screen_id_is_matched_case_insensitively_impl(self) -> None:
        """An operator typing the ID by hand must not be locked out."""
        screen = await self.make_screen(name="Front board", classroom=self.classroom)

        communicator = WebsocketCommunicator(
            application, screen_url(screen.screen_id.lower()), headers=origin_headers()
        )
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)
        self._open_communicators.append(communicator)

        self.assertTrue(connected)
        message = await receive_within(communicator)
        self.assertEqual(message["screen"]["screen_id"], screen.screen_id)

    def test_a_screen_with_no_classroom_is_refused(self) -> None:

        self._run(self._test_a_screen_with_no_classroom_is_refused_impl)


    async def _test_a_screen_with_no_classroom_is_refused_impl(self) -> None:
        screen = await self.make_screen(name="Spare board")

        communicator = WebsocketCommunicator(
            application, screen_url(screen.screen_id), headers=origin_headers()
        )
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)

        self.assertFalse(connected)


class TeacherConnectionTests(LiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.competition = create_competition(teacher=self.teacher)

    def test_the_owning_teacher_connects(self) -> None:

        self._run(self._test_the_owning_teacher_connects_impl)


    async def _test_the_owning_teacher_connects_impl(self) -> None:
        communicator, connected = await self._connect_teacher(
            self.teacher, self.competition
        )

        self.assertTrue(connected)
        message = await receive_within(communicator)
        self.assertEqual(message["type"], WELCOME)
        self.assertEqual(message["competition_id"], self.competition.pk)

    def test_an_anonymous_visitor_is_refused(self) -> None:

        self._run(self._test_an_anonymous_visitor_is_refused_impl)


    async def _test_an_anonymous_visitor_is_refused_impl(self) -> None:
        communicator = WebsocketCommunicator(
            application, teacher_url(self.competition.pk), headers=origin_headers()
        )
        communicator.scope = build_anonymous_teacher_scope(self.competition.pk)

        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)

        self.assertFalse(connected)

    def test_a_different_teacher_is_refused(self) -> None:

        self._run(self._test_a_different_teacher_is_refused_impl)


    async def _test_a_different_teacher_is_refused_impl(self) -> None:
        """Only the owner (or an administrator) may drive a round."""
        intruder = await self.make_teacher(username="other.teacher")

        communicator = WebsocketCommunicator(
            application, teacher_url(self.competition.pk), headers=origin_headers()
        )
        communicator.scope = await build_teacher_scope(intruder, self.competition.pk)
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)

        self.assertFalse(connected)

    def test_an_administrator_may_take_over(self) -> None:

        self._run(self._test_an_administrator_may_take_over_impl)


    async def _test_an_administrator_may_take_over_impl(self) -> None:
        """During a live lesson the administrator is often at the laptop."""
        administrator = await self._in_thread(create_administrator)

        communicator = WebsocketCommunicator(
            application, teacher_url(self.competition.pk), headers=origin_headers()
        )
        communicator.scope = await build_teacher_scope(
            administrator, self.competition.pk
        )
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)
        self._open_communicators.append(communicator)

        self.assertTrue(connected)


# ---------------------------------------------------------------------------
# Competition start and question broadcast
# ---------------------------------------------------------------------------


class CompetitionStartTests(LiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.classroom = create_classroom(name="Science Lab 1")
        self.competition = create_competition(
            teacher=self.teacher, classrooms=[self.classroom]
        )
        self.screen = create_screen(name="Front board", classroom=self.classroom)

    def test_starting_a_competition_broadcasts_to_every_screen(self) -> None:

        self._run(self._test_starting_a_competition_broadcasts_to_every_screen_impl)


    async def _test_starting_a_competition_broadcasts_to_every_screen_impl(self) -> None:
        """A screen joins a started round, and every one of them hears it open."""
        # There is nothing to join before the teacher presses start, so the
        # screen is refused outright rather than left waiting on a socket that
        # would never receive anything.
        _, connected_before_start = await self._connect_screen(self.screen)
        self.assertFalse(connected_before_start)

        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "start_competition"})
        await drain_until(teacher, COMPETITION_STARTED)

        extra_screens = [
            await self.make_screen(name=f"Board {index}", classroom=self.classroom)
            for index in range(1, 3)
        ]

        for screen in [self.screen, *extra_screens]:
            communicator, connected = await self._connect_screen(screen)
            self.assertTrue(connected)
            # The state sync on connect is what a screen needs to be correct
            # immediately, without waiting for the next teacher action.
            message, _ = await drain_until(communicator, STATE_SYNC)

            # A started round with no question open is WAITING; the round is
            # live but nothing is on screen yet.
            self.assertEqual(message["state"], CompetitionState.WAITING)
            self.assertEqual(message["competition_id"], self.competition.pk)

    def test_a_started_competition_records_the_start_time(self) -> None:

        self._run(self._test_a_started_competition_records_the_start_time_impl)


    async def _test_a_started_competition_records_the_start_time_impl(self) -> None:
        await self.start_competition(self.competition)

        await self.refresh(self.competition)

        self.assertIsNotNone(self.competition.started_at)
        self.assertEqual(self.competition.total_questions, 1)

    def test_a_competition_cannot_be_started_twice(self) -> None:

        self._run(self._test_a_competition_cannot_be_started_twice_impl)


    async def _test_a_competition_cannot_be_started_twice_impl(self) -> None:
        await self.start_competition(self.competition)

        with self.assertRaises(services.CompetitionControlError) as context:
            await self.start_competition(self.competition)

        self.assertEqual(context.exception.code, "already_started")

    def test_a_competition_with_no_classrooms_cannot_start(self) -> None:

        self._run(self._test_a_competition_with_no_classrooms_cannot_start_impl)


    async def _test_a_competition_with_no_classrooms_cannot_start_impl(self) -> None:
        competition = await self.make_competition(teacher=self.teacher, classrooms=[])

        with self.assertRaises(services.CompetitionControlError) as context:
            await self.start_competition(competition)

        self.assertEqual(context.exception.code, "no_classrooms")

    def test_a_competition_with_no_questions_cannot_start(self) -> None:

        self._run(self._test_a_competition_with_no_questions_cannot_start_impl)


    async def _test_a_competition_with_no_questions_cannot_start_impl(self) -> None:
        competition = await self.make_competition(
            teacher=self.teacher, classrooms=[self.classroom], questions=[]
        )

        with self.assertRaises(services.CompetitionControlError) as context:
            await self.start_competition(competition)

        self.assertEqual(context.exception.code, "no_questions")


class QuestionBroadcastTests(LiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.classroom = create_classroom(name="Science Lab 1")
        self.competition = create_competition(
            teacher=self.teacher, classrooms=[self.classroom]
        )
        self.question = self.competition.questions.first().question
        self.screen = create_screen(name="Front board", classroom=self.classroom)
        services.start_competition(self.competition)

    def test_a_started_question_reaches_the_screen(self) -> None:

        self._run(self._test_a_started_question_reaches_the_screen_impl)


    async def _test_a_started_question_reaches_the_screen_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)

        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "start_question"})

        message, _ = await drain_until(communicator, QUESTION_STARTED)

        self.assertEqual(message["question_id"], self.question.pk)
        self.assertEqual(message["text"], self.question.text)
        self.assertEqual(message["question_type"], self.question.question_type)
        self.assertEqual(message["options"], self.question.options)
        self.assertEqual(message["question_number"], 1)
        self.assertEqual(message["total_questions"], 1)
        self.assertIn("starts_at", message)
        self.assertIn("ends_at", message)
        self.assertIn("duration_seconds", message)

    def test_the_broadcast_never_carries_an_answer_key(self) -> None:

        self._run(self._test_the_broadcast_never_carries_an_answer_key_impl)


    async def _test_the_broadcast_never_carries_an_answer_key_impl(self) -> None:
        """A screen must not be able to read the answer off the wire."""
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await send_message(teacher, {"type": "start_question"})
        message, _ = await drain_until(communicator, QUESTION_STARTED)

        self.assertNotIn("correct_option", message)
        self.assertNotIn("correct_index", message)
        self.assertNotIn("answer", message)

    def test_the_image_is_included_when_present(self) -> None:

        self._run(self._test_the_image_is_included_when_present_impl)


    async def _test_the_image_is_included_when_present_impl(self) -> None:
        question = await self.make_question(text="Which shape?")
        question.image = "questions/sample.png"
        await self._in_thread(question.save, update_fields=["image"])
        # The question must belong to the round before the teacher can ask it.
        await self.add_question(self.competition, question, position=2)

        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await send_message(teacher, {"type": "start_question", "position": 2})
        message, _ = await drain_until(communicator, QUESTION_STARTED)

        self.assertEqual(message["question_id"], question.pk)

    def test_advancing_broadcasts_the_next_question(self) -> None:

        self._run(self._test_advancing_broadcasts_the_next_question_impl)


    async def _test_advancing_broadcasts_the_next_question_impl(self) -> None:
        await self.add_question(self.competition, await self.make_question(text="Second question"), position=2)

        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await send_message(teacher, {"type": "start_question"})
        await drain_until(communicator, QUESTION_STARTED)

        # Phase 6: advancing is only legal once the question is closed, so a
        # question can never be left without a result. The teacher ends it, the
        # result is revealed, and only then does the round move on.
        await send_message(teacher, {"type": "end_question"})
        await drain_until(communicator, ANSWERING_CLOSED)
        await drain_until(communicator, RESULT_REVEALED)

        await send_message(teacher, {"type": "advance"})
        message, _ = await drain_until(communicator, QUESTION_STARTED)

        self.assertEqual(message["question_number"], 2)
        self.assertEqual(message["text"], "Second question")

    def test_finishing_broadcasts_to_the_screen(self) -> None:

        self._run(self._test_finishing_broadcasts_to_the_screen_impl)


    async def _test_finishing_broadcasts_to_the_screen_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await send_message(teacher, {"type": "finish_competition"})
        message, _ = await drain_until(communicator, COMPETITION_FINISHED)

        self.assertEqual(message["state"], CompetitionState.FINISHED)

    def test_a_screen_cannot_drive_the_competition(self) -> None:

        self._run(self._test_a_screen_cannot_drive_the_competition_impl)


    async def _test_a_screen_cannot_drive_the_competition_impl(self) -> None:
        """Only the teacher moves a competition. A screen is told no."""
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": "start_question"})
        message, _ = await drain_until(communicator, "error")

        self.assertEqual(message["code"], "unknown_message")

        await self.refresh(self.competition)
        self.assertEqual(self.competition.question_number, 0)


# ---------------------------------------------------------------------------
# Multiple screens receive the same event
# ---------------------------------------------------------------------------


class MultipleScreenBroadcastTests(LiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.classrooms = [
            create_classroom(name=f"Lab {index}") for index in range(1, 4)
        ]
        self.competition = create_competition(
            teacher=self.teacher, classrooms=self.classrooms
        )
        self.screens = [
            create_screen(name=f"Board {index}", classroom=classroom)
            for index, classroom in enumerate(self.classrooms, start=1)
        ]
        services.start_competition(self.competition)

    def test_all_screens_receive_the_same_question(self) -> None:

        self._run(self._test_all_screens_receive_the_same_question_impl)


    async def _test_all_screens_receive_the_same_question_impl(self) -> None:
        communicators = []
        for screen in self.screens:
            communicator, _ = await self._connect_screen(screen)
            await self._skip_intro(communicator)
            communicators.append(communicator)

        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "start_question"})

        received = []
        for communicator in communicators:
            message, _ = await drain_until(communicator, QUESTION_STARTED)
            received.append(message)

        first = received[0]
        for message in received[1:]:
            for field in ("question_id", "text", "options", "ends_at", "starts_at"):
                self.assertEqual(
                    message[field],
                    first[field],
                    f"Screens disagreed about {field}.",
                )

    def test_all_screens_share_one_server_deadline(self) -> None:

        self._run(self._test_all_screens_share_one_server_deadline_impl)


    async def _test_all_screens_share_one_server_deadline_impl(self) -> None:
        """The deadline is absolute, so no screen can have its own timer."""
        communicators = []
        for screen in self.screens:
            communicator, _ = await self._connect_screen(screen)
            await self._skip_intro(communicator)
            communicators.append(communicator)

        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "start_question"})

        deadlines = set()
        for communicator in communicators:
            message, _ = await drain_until(communicator, QUESTION_STARTED)
            deadlines.add(message["ends_at"])

        self.assertEqual(len(deadlines), 1)

    def test_the_teacher_sees_how_many_screens_are_connected(self) -> None:

        self._run(self._test_the_teacher_sees_how_many_screens_are_connected_impl)


    async def _test_the_teacher_sees_how_many_screens_are_connected_impl(self) -> None:
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        for screen in self.screens:
            communicator, _ = await self._connect_screen(screen)
            await self._skip_intro(communicator)

        message, _ = await drain_until(
            teacher, "presence", match=lambda m: m["connected_screens"] == 3
        )

        self.assertEqual(message["connected_screens"], 3)

    def test_a_disconnect_reduces_the_connected_count(self) -> None:

        self._run(self._test_a_disconnect_reduces_the_connected_count_impl)


    async def _test_a_disconnect_reduces_the_connected_count_impl(self) -> None:
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        communicators = []
        for screen in self.screens:
            communicator, _ = await self._connect_screen(screen)
            await self._skip_intro(communicator)
            communicators.append(communicator)

        await drain_until(teacher, "presence")
        await communicators[0].disconnect()

        message, _ = await drain_until(teacher, "presence")
        self.assertEqual(message["connected_screens"], 2)


# ---------------------------------------------------------------------------
# Timer state - the server is authoritative
# ---------------------------------------------------------------------------


class TimerStateTests(LiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.classroom = create_classroom(name="Science Lab 1")
        self.competition = create_competition(
            teacher=self.teacher, classrooms=[self.classroom]
        )
        self.screen = create_screen(name="Front board", classroom=self.classroom)
        services.start_competition(self.competition)

    def test_starting_a_question_sets_an_absolute_deadline(self) -> None:
        services.start_question(self.competition)

        self.competition.refresh_from_db()

        self.assertIsNotNone(self.competition.current_question_started_at)
        self.assertIsNotNone(self.competition.current_question_ends_at)
        self.assertGreater(
            self.competition.current_question_ends_at,
            self.competition.current_question_started_at,
        )
        self.assertEqual(self.competition.state, CompetitionState.QUESTION_ACTIVE)

    def test_the_deadline_equals_the_configured_duration(self) -> None:
        entry = self.competition.questions.first()

        services.start_question(self.competition)

        self.competition.refresh_from_db()
        remaining = self.competition.seconds_remaining()

        self.assertLessEqual(remaining, entry.effective_duration_seconds)
        self.assertGreater(remaining, entry.effective_duration_seconds - 5)

    def test_a_competition_override_changes_the_deadline(self) -> None:
        entry = self.competition.questions.first()
        entry.duration_seconds = 90
        entry.save(update_fields=["duration_seconds"])

        services.start_question(self.competition)

        self.competition.refresh_from_db()
        remaining = self.competition.seconds_remaining()

        self.assertGreater(remaining, 85)

    def test_seconds_remaining_never_goes_negative(self) -> None:
        services.start_question(self.competition)

        from datetime import timedelta

        from django.utils import timezone

        long_gone = timezone.now() + timedelta(seconds=600)
        self.assertEqual(self.competition.seconds_remaining(long_gone), 0.0)

    def test_a_tick_carries_the_server_time_and_remaining_seconds(self) -> None:

        self._run(self._test_a_tick_carries_the_server_time_and_remaining_seconds_impl)


    async def _test_a_tick_carries_the_server_time_and_remaining_seconds_impl(self) -> None:
        """The beacon is what lets a browser correct its own clock."""
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": "ping"})
        message, _ = await drain_until(communicator, "tick")

        self.assertIn("server_time", message)
        self.assertIn("seconds_remaining", message)
        self.assertIsInstance(message["server_time"], str)

    def test_the_server_closes_answering_when_time_is_up(self) -> None:

        self._run(self._test_the_server_closes_answering_when_time_is_up_impl)


    async def _test_the_server_closes_answering_when_time_is_up_impl(self) -> None:
        """The deadline is enforced by the server, not by any browser."""
        from datetime import timedelta

        from channels.layers import get_channel_layer
        from django.utils import timezone

        entry = await self._in_thread(self.competition.questions.first)
        entry.duration_seconds = 5
        await self._in_thread(entry.save, update_fields=["duration_seconds"])
        await self.start_question(self.competition)

        await self.refresh(self.competition)
        # Force the deadline into the past so the clock fires immediately.
        await self._in_thread(
            Competition.objects.filter(pk=self.competition.pk).update,
            current_question_ends_at=timezone.now() - timedelta(seconds=1),
        )

        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await clock_runner.start_clock(
            self.competition.pk,
            get_channel_layer(),
            timezone.now() - timedelta(seconds=1),
        )

        message, _ = await drain_until(communicator, ANSWERING_CLOSED)
        self.assertEqual(message["state"], CompetitionState.ANSWERING_CLOSED)
        self.assertEqual(message["seconds_remaining"], 0.0)

    def test_close_answering_is_refused_when_no_question_is_open(self) -> None:
        with self.assertRaises(services.CompetitionControlError) as context:
            services.close_answering(self.competition)

        self.assertEqual(context.exception.code, "not_answering")


# ---------------------------------------------------------------------------
# Reconnection
# ---------------------------------------------------------------------------


class ReconnectionTests(LiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.classroom = create_classroom(name="Science Lab 1")
        self.competition = create_competition(
            teacher=self.teacher, classrooms=[self.classroom]
        )
        self.screen = create_screen(name="Front board", classroom=self.classroom)
        services.start_competition(self.competition)

    def test_a_screen_can_reconnect_and_receive_current_state(self) -> None:

        self._run(self._test_a_screen_can_reconnect_and_receive_current_state_impl)


    async def _test_a_screen_can_reconnect_and_receive_current_state_impl(self) -> None:
        """A dropped screen rejoins mid-question with the time remaining."""
        first, _ = await self._connect_screen(self.screen)
        await self._skip_intro(first)

        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "start_question"})
        await drain_until(first, QUESTION_STARTED)

        await first.disconnect()

        # The browser reconnects and presents the same Screen ID.
        second, connected = await self._connect_screen(self.screen)

        self.assertTrue(connected)
        message, _ = await drain_until(second, STATE_SYNC)

        self.assertEqual(message["state"], CompetitionState.QUESTION_ACTIVE)
        self.assertEqual(message["question_number"], 1)
        self.assertGreater(message["seconds_remaining"], 0)
        self.assertEqual(message["screen"]["screen_id"], self.screen.screen_id)

    def test_a_reconnecting_screen_receives_the_current_question(self) -> None:

        self._run(self._test_a_reconnecting_screen_receives_the_current_question_impl)


    async def _test_a_reconnecting_screen_receives_the_current_question_impl(self) -> None:
        first, _ = await self._connect_screen(self.screen)
        await self._skip_intro(first)
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "start_question"})
        await drain_until(first, QUESTION_STARTED)

        await first.disconnect()
        second, _ = await self._connect_screen(self.screen)

        message, _ = await drain_until(second, STATE_SYNC)

        await self.refresh(self.competition)
        self.assertEqual(
            message["question"]["question_id"],
            self.competition.current_question_id,
        )

    def test_a_screen_can_re_identify_on_a_live_socket(self) -> None:

        self._run(self._test_a_screen_can_re_identify_on_a_live_socket_impl)


    async def _test_a_screen_can_re_identify_on_a_live_socket_impl(self) -> None:
        """Re-identifying without reconnecting also restores state."""
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)

        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "start_question"})
        await drain_until(communicator, QUESTION_STARTED)

        await send_message(
            communicator, {"type": "identify", "screen_id": self.screen.screen_id}
        )

        message, _ = await drain_until(communicator, STATE_SYNC)
        self.assertEqual(message["question_number"], 1)

    def test_a_rejected_reidentify_is_answered_not_silently_ignored(self) -> None:

        self._run(self._test_a_rejected_reidentify_is_answered_not_silently_ignored_impl)


    async def _test_a_rejected_reidentify_is_answered_not_silently_ignored_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)

        await send_message(communicator, {"type": "identify", "screen_id": "AM-ZZZZZZ"})
        message, _ = await drain_until(communicator, REJECTED)

        self.assertEqual(message["code"], "unregistered_screen")


# ---------------------------------------------------------------------------
# Unauthorised classrooms
# ---------------------------------------------------------------------------


class UnauthorizedClassroomTests(LiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.included = create_classroom(name="Included Lab")
        self.excluded = create_classroom(name="Excluded Lab")
        self.competition = create_competition(
            teacher=self.teacher, classrooms=[self.included]
        )
        services.start_competition(self.competition)

    def test_a_screen_from_an_included_classroom_may_join(self) -> None:

        self._run(self._test_a_screen_from_an_included_classroom_may_join_impl)


    async def _test_a_screen_from_an_included_classroom_may_join_impl(self) -> None:
        screen = await self.make_screen(name="Front board", classroom=self.included)

        communicator, connected = await self._connect_screen(screen)

        self.assertTrue(connected)

    def test_a_screen_from_an_excluded_classroom_is_refused(self) -> None:

        self._run(self._test_a_screen_from_an_excluded_classroom_is_refused_impl)


    async def _test_a_screen_from_an_excluded_classroom_is_refused_impl(self) -> None:
        """Registered and healthy, but its classroom was not included."""
        screen = await self.make_screen(name="Secret board", classroom=self.excluded)

        communicator = WebsocketCommunicator(
            application, screen_url(screen.screen_id), headers=origin_headers()
        )
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)

        self.assertFalse(connected)

    def test_an_excluded_screen_receives_no_question(self) -> None:

        self._run(self._test_an_excluded_screen_receives_no_question_impl)


    async def _test_an_excluded_screen_receives_no_question_impl(self) -> None:
        screen = await self.make_screen(name="Secret board", classroom=self.excluded)
        communicator = WebsocketCommunicator(
            application, screen_url(screen.screen_id), headers=origin_headers()
        )
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)
        self.assertFalse(connected)

        # The included screen is attached first, so it is genuinely waiting for
        # the broadcast rather than joining after the fact.
        included_screen = await self.make_screen(
            name="Front board", classroom=self.included
        )
        listener, _ = await self._connect_screen(included_screen)
        await self._skip_intro(listener)

        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)
        await send_message(teacher, {"type": "start_question"})

        message, _ = await drain_until(listener, QUESTION_STARTED)

        self.assertEqual(message["competition_id"], self.competition.pk)

    def test_includes_classroom_is_the_authorisation_check(self) -> None:
        self.assertTrue(self.competition.includes_classroom(self.included.pk))
        self.assertFalse(self.competition.includes_classroom(self.excluded.pk))
        self.assertFalse(self.competition.includes_classroom(None))

    def test_an_inactive_classroom_cannot_join(self) -> None:

        self._run(self._test_an_inactive_classroom_cannot_join_impl)


    async def _test_an_inactive_classroom_cannot_join_impl(self) -> None:
        self.included.active = False
        await self._in_thread(self.included.save, update_fields=["active"])
        screen = await self.make_screen(name="Front board", classroom=self.included)

        communicator = WebsocketCommunicator(
            application, screen_url(screen.screen_id), headers=origin_headers()
        )
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)

        self.assertFalse(connected)

    def test_a_screen_cannot_choose_which_competition_it_joins(self) -> None:

        self._run(self._test_a_screen_cannot_choose_which_competition_it_joins_impl)


    async def _test_a_screen_cannot_choose_which_competition_it_joins_impl(self) -> None:
        """There is no competition id in the screen URL to nominate."""
        screen = await self.make_screen(name="Front board", classroom=self.included)
        other = await self.make_competition(teacher=await self.make_teacher(username="other.host"))

        communicator = WebsocketCommunicator(
            application, f"/ws/live/screen/?screen_id={screen.screen_id}", headers=origin_headers()
        )
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)
        self._open_communicators.append(communicator)

        self.assertTrue(connected)
        message = await receive_within(communicator)
        self.assertEqual(message["competition_id"], self.competition.pk)
        self.assertNotEqual(message["competition_id"], other.pk)

# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------


class HeartbeatTests(LiveTestCase):
    """An idle screen must stay in its group.

    A group membership lapses if nothing passes through it, and a screen that
    has been sitting still between two teacher actions is exactly the screen
    that would silently stop updating. The keepalive is what prevents that.
    """

    def setUp(self) -> None:
        super().setUp()
        self.teacher = create_teacher()
        self.classroom = create_classroom(name="Science Lab 1")
        self.competition = create_competition(
            teacher=self.teacher, classrooms=[self.classroom]
        )
        self.screen = create_screen(name="Front board", classroom=self.classroom)
        services.start_competition(self.competition)

    def test_the_interval_is_shorter_than_the_group_expiry(self) -> None:
        """Otherwise the backstop lapses before the keepalive can fire."""
        from django.conf import settings

        self.assertLess(
            heartbeat.heartbeat_interval(),
            settings.CHANNEL_LAYER_EXPIRY_SECONDS,
        )

    def test_rounds_are_discovered_from_attached_screens(self) -> None:
        self.assertEqual(heartbeat.live_competition_ids(), set())

        presence.register_member(
            groups.screen_group(self.competition.pk), "specific.channel"
        )

        self.assertEqual(
            heartbeat.live_competition_ids(), {self.competition.pk}
        )

    def test_an_idle_screen_is_still_reached_by_a_keepalive(self) -> None:

        self._run(self._test_an_idle_screen_is_still_reached_by_a_keepalive_impl)


    async def _test_an_idle_screen_is_still_reached_by_a_keepalive_impl(self) -> None:
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)

        # Nothing happens for a while, exactly as between two questions.
        sent = await heartbeat.broadcast_once(self._channel_layer())

        self.assertEqual(sent, 1)
        message, _ = await drain_until(communicator, TICK)
        self.assertIsNotNone(message)
        self.assertIsNotNone(message["server_time"])
        self.assertEqual(message["state"], CompetitionState.WAITING)

    def test_a_keepalive_reaches_the_teacher_too(self) -> None:

        self._run(self._test_a_keepalive_reaches_the_teacher_too_impl)


    async def _test_a_keepalive_reaches_the_teacher_too_impl(self) -> None:
        teacher, _ = await self._connect_teacher(self.teacher, self.competition)
        await self._skip_intro(teacher)

        await heartbeat.broadcast_once(
            self._channel_layer(), competition_ids=[self.competition.pk]
        )

        message, _ = await drain_until(teacher, TICK)
        self.assertIsNotNone(message)
        self.assertEqual(message["question_number"], 0)

    def test_a_round_with_no_screens_is_not_touched(self) -> None:
        from channels.layers import get_channel_layer

        self.assertEqual(
            async_to_sync(heartbeat.broadcast_once)(get_channel_layer()), 0
        )

    def test_connecting_starts_the_keepalive(self) -> None:

        self._run(self._test_connecting_starts_the_keepalive_impl)


    async def _test_connecting_starts_the_keepalive_impl(self) -> None:
        self.assertFalse(heartbeat.is_running())

        communicator, connected = await self._connect_screen(self.screen)
        self.assertTrue(connected)
        # connect() returns as soon as the socket is accepted, so wait for the
        # rest of the connect handler before inspecting what it started.
        await self._skip_intro(communicator)

        self.assertTrue(heartbeat.is_running())

    def test_the_tick_carries_the_deadline_while_answering(self) -> None:

        self._run(self._test_the_tick_carries_the_deadline_while_answering_impl)


    async def _test_the_tick_carries_the_deadline_while_answering_impl(self) -> None:
        await self.start_question(self.competition)
        communicator, _ = await self._connect_screen(self.screen)
        await self._skip_intro(communicator)

        await heartbeat.broadcast_once(self._channel_layer())

        message, _ = await drain_until(communicator, TICK)
        self.assertIsNotNone(message)
        self.assertEqual(message["state"], CompetitionState.QUESTION_ACTIVE)
        self.assertGreater(message["seconds_remaining"], 0)

    @staticmethod
    def _channel_layer():
        from channels.layers import get_channel_layer

        return get_channel_layer()
# ---------------------------------------------------------------------------
# Channel layer configuration
# ---------------------------------------------------------------------------


class ChannelLayerConfigurationTests(SimpleTestCase):
    """Guards the settings that keep live sockets alive.

    These are settings rather than code, which means a dependency bump or a
    well-meaning edit can undo them silently. Each test here corresponds to a
    way that has actually happened.
    """

    def test_the_redis_client_has_no_socket_timeout(self) -> None:
        """A client-side read timeout drops every idle WebSocket.

        The layer blocks on BZPOPMIN for `brpop_timeout` seconds; redis-py 8
        defaults `socket_timeout` to 5s, so the read gives up first and takes
        the connection down with it.
        """
        from django.conf import settings

        hosts = settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"]
        self.assertTrue(hosts, "the channel layer needs a Redis host")
        for host in hosts:
            self.assertIn(
                "socket_timeout",
                host,
                "an explicit socket_timeout keeps redis-py's default out of the "
                "channel layer",
            )
            self.assertIsNone(
                host["socket_timeout"],
                "the channel layer must block on its own brpop_timeout, not on a "
                "client-side read timeout",
            )

    def test_the_block_and_the_read_timeout_cannot_collide(self) -> None:
        """Guards the mechanism, whatever the versions do by default."""
        from django.conf import settings

        config = settings.CHANNEL_LAYERS["default"]["CONFIG"]
        layer = RedisChannelLayer(
            hosts=config["hosts"],
            capacity=config["capacity"],
            expiry=config["expiry"],
        )

        # channels_redis bounds each blocking read by `expiry`; a client-side
        # timeout at or below that turns an ordinary idle wait into an error.
        self.assertGreater(layer.brpop_timeout, 0)
        self.assertGreaterEqual(layer.brpop_timeout, 1)
        for host in config["hosts"]:
            self.assertIsNone(host.get("socket_timeout"))

    def test_the_group_expiry_outlives_the_keepalive(self) -> None:
        from django.conf import settings

        self.assertGreater(
            settings.CHANNEL_LAYER_EXPIRY_SECONDS,
            settings.LIVE_HEARTBEAT_SECONDS,
            "an expired group membership silently stops a screen receiving "
            "broadcasts, so the backstop must outlive the keepalive that "
            "normally prevents it",
        )