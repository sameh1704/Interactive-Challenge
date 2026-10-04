"""The teacher dashboard WebSocket consumer.

Authentication differs from the screen consumer in one important way: a teacher
signs in over HTTP and arrives with a Django session, so this consumer is
authorised from ``self.scope["user"]`` rather than from anything it sends. A
client cannot elevate itself by sending a message.

Authorisation, in order:

1. The user must be authenticated staff. Anonymous sockets are closed before
   ``accept()``, so no unauthorised connection is ever established.
2. The user must be the teacher who owns the competition, or an administrator
   who may take over.

The teacher is the only party that may move a competition's state. Screens never
send control messages; if one does, it is answered with an error and ignored.
"""

from __future__ import annotations

import json
import logging

from channels.db import database_sync_to_async
from django.utils import timezone

from competitions.models import Competition
from live import events, groups, services
from live.consumers_base import CLOSE_UNAUTHORISED, BaseLiveConsumer, error_payload
from live.services import CompetitionControlError

logger = logging.getLogger(__name__)


class TeacherConsumer(BaseLiveConsumer):
    """A signed-in member of staff running a competition."""

    async def connect(self) -> None:
        self.user = self.scope.get("user")
        self.competition_id = None
        self.competition = None
        self.group = None

        if not await self._is_staff():
            await self.close(code=CLOSE_UNAUTHORISED)
            return

        self.competition_id = await self._competition_id_from_scope()
        competition = await self._load_competition()

        if competition is None:
            await self.close(code=CLOSE_UNAUTHORISED)
            return

        try:
            services.assert_can_control(competition, self.user)
        except CompetitionControlError as error:
            await self.close(code=CLOSE_UNAUTHORISED)
            logger.info("Teacher WebSocket refused: %s", error.code)
            return

        await self.accept()
        self.competition = competition
        self.group = groups.teacher_group(competition.pk)
        await self.channel_layer.group_add(self.group, self.channel_name)

        await self.send_json(
            {
                "type": events.WELCOME,
                "competition_id": competition.pk,
                "competition_title": competition.title,
                "state": competition.state,
                "server_time": timezone.now().isoformat(),
            }
        )

        await self._send_state_sync()

        # Keep this round's group memberships warm while the teacher thinks
        # about the next question. See live.heartbeat.
        from live import heartbeat

        await heartbeat.ensure_running(self.channel_layer)

    async def disconnect(self, code) -> None:
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)
        logger.info("Teacher WebSocket closed (code=%s).", code)

    async def receive(self, text_data: str = None, bytes_data: bytes = None) -> None:
        if not text_data:
            return
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json(error_payload("bad_json", "Malformed message."))
            return

        if not isinstance(message, dict):
            await self.send_json(error_payload("bad_json", "Expected an object."))
            return

        await self._route(message)

    async def _route(self, message: dict) -> None:
        kind = message.get("type")

        handlers = {
            events.START_COMPETITION: self._handle_start,
            events.START_QUESTION: self._handle_start_question,
            events.END_QUESTION: self._handle_end_question,
            events.ADVANCE: self._handle_advance,
            events.FINISH_COMPETITION: self._handle_finish,
        }

        handler = handlers.get(kind)
        if handler is None:
            await self.send_json(
                error_payload("unknown_message", f"Unsupported message: {kind!r}")
            )
            return

        try:
            await handler(message)
        except CompetitionControlError as error:
            # A refused action is reported back to the teacher, and recorded here.
            # The teacher's dashboard explains itself well enough that a refused
            # action looks like ordinary use; without this line there is no way to
            # tell afterwards that a round was being driven into an invalid state.
            logger.warning(
                "Competition control refused (%s): %s",
                error.code,
                error.message,
            )
            await self.send_json(
                {
                    "type": events.ERROR,
                    "code": error.code,
                    "message": error.message,
                }
            )

    # -- control actions ---------------------------------------------------
    #
    # Every service call touches the database, so each one runs through
    # `database_sync_to_async`. Calling them directly here would raise
    # SynchronousOnlyOperation, because this consumer runs in the event loop.

    async def _handle_start(self, message: dict) -> None:
        transition = await self._run(services.start_competition)
        await self._apply(transition)

    async def _handle_start_question(self, message: dict) -> None:
        position = message.get("position")
        transition = await self._run(services.start_question, position=position)
        await self._apply(transition)

    async def _handle_end_question(self, message: dict) -> None:
        """End the open question now, without skipping to the next one.

        Everything downstream of this - the ``answering_closed`` broadcast, the
        clock being cancelled and the result reveal - is driven by the transition
        the service returns, so a teacher pressing this button and the deadline
        arriving produce the same thing in the same order.
        """
        transition = await self._run(services.end_question)
        await self._apply(transition)

    async def _handle_advance(self, message: dict) -> None:
        transition = await self._run(services.advance)
        await self._apply(transition)

    async def _handle_finish(self, message: dict) -> None:
        transition = await self._run(services.finish_competition)
        await self._apply(transition)

    async def _run(self, func, **kwargs):
        """Call a control service with a freshly loaded competition."""

        @database_sync_to_async
        def _invoke():
            competition = (
                Competition.objects.filter(pk=self.competition_id)
                .select_related("current_question", "teacher")
                .prefetch_related("questions__question")
                .first()
            )
            if competition is None:
                raise CompetitionControlError(
                    "missing_competition", "This competition no longer exists."
                )
            services.assert_can_control(competition, self.user)
            return func(competition, **kwargs)

        return await _invoke()

    async def _apply(self, transition) -> None:
        """Broadcast a transition to every screen and the teacher group.

        The screens receive the payload untouched. The state has already been
        committed by the time we get here, so a client that reconnects
        immediately reads the same state from the database.
        """
        if transition is None:
            return

        competition_id = transition.competition.pk
        payload = {
            "type": transition.event,
            **transition.payload,
        }

        await self.channel_layer.group_send(
            groups.screen_group(competition_id), {"type": transition.event, "payload": payload}
        )
        await self.channel_layer.group_send(
            groups.teacher_group(competition_id),
            {"type": transition.event, "payload": payload},
        )

        # Arm or cancel the server clock to match the new state. This is what
        # makes the timing authoritative rather than browser-driven.
        await self._sync_clock(transition)

        await self._publish_scoring(transition)

        await self._send_state_sync()

    async def _publish_scoring(self, transition) -> None:
        """Release whatever scoring may now be shown.

        The reveal is tied to the answering-closed transition rather than to a
        button, so it happens whether the teacher closed the question or the
        server clock did. Nothing is published while a question is open.
        """
        from scoring import realtime

        if transition.event == events.ANSWERING_CLOSED:
            await realtime.broadcast_result_revealed(
                self.channel_layer, transition.competition.pk
            )
        elif transition.event == events.COMPETITION_FINISHED:
            await realtime.broadcast_final_results(
                self.channel_layer, transition.competition.pk
            )

    async def _sync_clock(self, transition) -> None:
        """Start the deadline timer for a new question, or stop it."""
        from live import clock_runner

        competition = transition.competition

        if transition.event == events.QUESTION_STARTED:
            await clock_runner.start_clock(
                competition.pk, self.channel_layer, competition.current_question_ends_at
            )
        else:
            clock_runner.cancel_clock(competition.pk)

    # -- state -------------------------------------------------------------

    async def _send_state_sync(self) -> None:
        competition = await self._load_competition()
        if competition is None:
            return

        from live.presence import count_group_members

        connected = await count_group_members(
            self.channel_layer, groups.screen_group(competition.pk)
        )

        await self.send_json(
            events.state_payload(competition, connected_screens=connected)
        )

    async def presence_changed(self, event) -> None:
        await self.send_json(event["payload"])

    async def competition_started(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.COMPETITION_STARTED})

    async def question_started(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.QUESTION_STARTED})

    async def answering_closed(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.ANSWERING_CLOSED})

    async def next_question(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.NEXT_QUESTION})

    async def competition_finished(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.COMPETITION_FINISHED})

    async def result_revealed(self, event) -> None:
        """The question's outcome, for the teacher's dashboard.

        This must exist even though the teacher also caused the reveal. Channels
        dispatches a group message to a method named after its type and raises
        ``ValueError`` if there is none, so a handler that is "never needed"
        because the event arrives unsolicited is still mandatory. Without it the
        teacher's socket is torn down by the first reveal, and every later
        control silently does nothing.
        """
        await self.send_json({**event["payload"], "type": events.RESULT_REVEALED})

    async def leaderboard_updated(self, event) -> None:
        await self.send_json(
            {**event["payload"], "type": events.LEADERBOARD_UPDATED}
        )

    async def competition_results(self, event) -> None:
        await self.send_json(
            {**event["payload"], "type": events.COMPETITION_RESULTS}
        )

    async def tick(self, event) -> None:
        """The keepalive.

        The teacher's dashboard shows the same countdown as the screens, so it
        needs the same corrected clock. Receiving this also keeps the teacher's
        own group membership warm.
        """
        payload = event.get("payload", event)
        await self.send_json({**payload, "type": events.TICK})

    # -- database access ---------------------------------------------------

    @database_sync_to_async
    def _is_staff(self) -> bool:
        from accounts.permissions import is_staff_member

        return is_staff_member(self.user)

    @database_sync_to_async
    def _competition_id_from_scope(self):
        from urllib.parse import unquote

        raw = (self.scope.get("url_route", {}).get("kwargs", {}) or {}).get(
            "competition_id"
        )
        if raw is None:
            return None
        try:
            return int(unquote(str(raw)))
        except (TypeError, ValueError):
            return None

    @database_sync_to_async
    def _load_competition(self):
        if not self.competition_id:
            return None
        return (
            Competition.objects.filter(pk=self.competition_id)
            .select_related("current_question", "teacher")
            .prefetch_related("questions__question")
            .first()
        )
