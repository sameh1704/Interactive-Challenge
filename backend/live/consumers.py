"""The classroom screen WebSocket consumer.

Connection flow
---------------
1. The screen opens ``/screen/?screen_id=AM-7KQ4XB``.
2. The page opens a WebSocket to ``/ws/live/screen/?screen_id=AM-7KQ4XB`` and
   also sends an ``identify`` message, so the screen is identified whether it
   reconnects into a fresh socket or re-identifies into a live one.
3. The server validates: the screen exists, the screen is active, and the
   screen's classroom is active.
4. The screen is added to that competition's group **only if its classroom was
   included in the competition**. A valid screen from a classroom that was not
   included is refused - see :meth:`ScreenConsumer._authorise_competition`.

Reconnection
------------
The browser script reconnects automatically and re-presents its Screen ID. On
every successful connection the server sends ``state_sync`` with the current
competition state and the current question, so a screen that was disconnected
for part of a question rejoins mid-question with the correct time remaining. No
manual refresh is involved and no state is lost on the server.
"""

from __future__ import annotations

import json
import logging

from channels.db import database_sync_to_async
from django.utils import timezone

from live import events, groups, services
from live.clock import remaining_seconds
from live.consumers_base import (
    CLOSE_UNAUTHORISED,
    BaseLiveConsumer,
    error_payload,
    normalise_screen_id,
    reject_payload,
)

logger = logging.getLogger(__name__)

# How often an attached screen sends/receives a clock beacon, in seconds. This
# keeps every screen's countdown aligned without flooding the socket.
DEFAULT_TICK_SECONDS = 5


class ScreenConsumer(BaseLiveConsumer):
    """A classroom's interactive screen, connected to one competition."""

    async def connect(self) -> None:
        # Initialised before anything can fail, so an early return never leaves
        # the instance without the attributes disconnect() relies on.
        self.screen_id = ""
        self.screen = None
        self.competition = None
        self.competition_id = None
        self.group = None
        self.connected = False
        self.accepted = False

        # A screen's identity arrives as a query parameter. It is accepted at
        # connect time and re-checked on the `identify` message.
        self.screen_id = normalise_screen_id(self._query_param("screen_id"))

        if not self.screen_id:
            # Nothing identifies this connection. Refuse before accepting so no
            # anonymous socket is ever established.
            await self.close(code=CLOSE_UNAUTHORISED)
            return

        result = await self._identify(self.screen_id)
        if not result["ok"]:
            await self.close(code=CLOSE_UNAUTHORISED)
            return

        await self.accept()
        self.accepted = True
        await self._join_and_welcome(result)

    async def disconnect(self, code) -> None:
        self.connected = False
        if self.competition_id and self.accepted:
            from live.presence import unregister_member

            unregister_member(groups.screen_group(self.competition_id), self.channel_name)
            await self._announce_presence()
        logger.info("Screen WebSocket closed (code=%s).", code)

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

        if kind == events.IDENTIFY:
            await self._handle_identify(message)
        elif kind == events.SUBMIT_ANSWER:
            await self._handle_submit_answer(message)
        elif kind == "ping":
            await self._send_tick()
        else:
            await self.send_json(
                error_payload("unknown_message", f"Unsupported message: {kind!r}")
            )

    async def _handle_submit_answer(self, message: dict) -> None:
        """Record this classroom's answer to the question on screen.

        The classroom and screen come from the authenticated connection, never
        from the message; the time comes from the server clock at receipt; the
        score is computed by :mod:`scoring.services`. A message carrying its own
        ``score``, ``is_correct``, ``response_time`` or ``classroom_id`` has those
        keys ignored rather than rejected, because rejecting them would confirm
        to a prober which fields are load-bearing.
        """
        from scoring import realtime
        from scoring.services import AnswerRejected, extract_selection

        if not self.connected or self.competition_id is None or self.screen is None:
            await self.send_json(
                error_payload("not_connected", "Identify this screen first.")
            )
            return

        try:
            selection = extract_selection(message)
        except AnswerRejected as error:
            await self.send_json(
                error_payload(error.code, error.message)
            )
            return

        try:
            answer = await self._submit(
                self.competition_id, self.screen.pk, self.screen.classroom_id, selection
            )
        except AnswerRejected as error:
            await self.send_json(error_payload(error.code, error.message))
            return

        # An acknowledgement, not a verdict. Telling a screen it was correct
        # before the answer period ends would hand the answer to every other
        # screen that watches, so the submitter learns nothing either.
        await self.send_json(
            {
                "type": events.ANSWER_ACCEPTED,
                "position": answer["position"],
                "server_time": answer["server_time"],
                "response_time_seconds": answer["response_time_seconds"],
            }
        )

        await realtime.broadcast_answer_progress(self.channel_layer, self.competition_id)

    async def _handle_identify(self, message: dict) -> None:
        """Re-identify an already-open socket.

        This is what makes reconnection work without closing the socket: a screen
        whose page reloaded can present its ID again on a live connection.
        """
        screen_id = normalise_screen_id(message.get("screen_id"))
        result = await self._identify(screen_id)

        if not result["ok"]:
            await self.send_json(
                reject_payload(
                    result["code"],
                    result["message"],
                )
            )
            return

        await self._join_and_welcome(result)

    async def _identify(self, screen_id: str) -> dict:
        """Validate a Screen ID and find the competition it may join.

        The checks are, in order: the presentation is within the rate limit; the
        screen exists and is active; its classroom is active; and that classroom
        was included in the competition. An unknown Screen ID and a deactivated
        one produce the same result, so the socket cannot be used to probe which
        IDs exist.
        """
        # The Screen ID is the only credential this socket has, so an unlimited
        # number of sockets guessing IDs would be an unlimited number of guesses.
        # Counted per client address; see core.ratelimit for why the limit is
        # generous enough for a whole classroom behind one NAT address.
        if not await self._within_rate_limit():
            return {
                "ok": False,
                "code": "unregistered_screen",
                "message": "This screen is not registered.",
            }

        screen = await self._load_screen(screen_id)
        if screen is None:
            logger.info("Screen WebSocket presented an unregistered Screen ID.")
            return {
                "ok": False,
                "code": "unregistered_screen",
                "message": "This screen is not registered.",
            }

        if screen.classroom is None:
            return {
                "ok": False,
                "code": "unassigned_screen",
                "message": "This screen is not assigned to a classroom.",
            }

        if not screen.classroom.active:
            return {
                "ok": False,
                "code": "inactive_classroom",
                "message": "This classroom is not active.",
            }

        competition = await self._active_competition()
        if competition is None:
            return {
                "ok": False,
                "code": "no_active_competition",
                "message": "There is no competition waiting for screens.",
            }

        if not await self._classroom_included(competition.pk, screen.classroom_id):
            # The authorisation boundary. The screen is registered and healthy,
            # but its classroom was not included in this round.
            return {
                "ok": False,
                "code": "classroom_not_included",
                "message": (
                    "This classroom is not taking part in this competition."
                ),
            }

        return {
            "ok": True,
            "screen": screen,
            "competition": competition,
        }

    async def _join_and_welcome(self, result: dict) -> None:
        screen = result["screen"]
        competition = result["competition"]
        previous_group = self.group if self.competition_id else None

        self.screen = screen
        self.competition = competition
        self.competition_id = competition.pk
        self.connected = True
        self.group = groups.screen_group(competition.pk)

        if previous_group and previous_group != self.group:
            await self.channel_layer.group_discard(previous_group, self.channel_name)

        await self.channel_layer.group_add(self.group, self.channel_name)
        from live.presence import register_member

        register_member(self.group, self.channel_name)

        await self.send_json(
            {
                "type": events.WELCOME,
                "screen": {
                    "screen_id": screen.screen_id,
                    "name": screen.name,
                    "classroom": screen.classroom.display_name,
                },
                "competition_id": competition.pk,
                "competition_title": competition.title,
                "state": competition.state,
                "server_time": timezone.now().isoformat(),
            }
        )

        # Restore the current state so a reconnecting screen is correct at once
        # instead of waiting for the next event.
        await self._send_state_sync()
        await self._announce_presence()

        # Hold the round together while it is idle. See live.heartbeat for why a
        # screen that is behaving correctly still needs this.
        from live import heartbeat

        await heartbeat.ensure_running(self.channel_layer)

    # -- outbound event handlers -------------------------------------------

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

    async def presence_changed(self, event) -> None:
        """Presence is teacher-facing, so a screen ignores it."""
        return

    async def answer_progress(self, event) -> None:
        """How many classrooms have answered so far.

        Deliberately only a count: it is broadcast while answering is open, and
        anything that distinguished correct from incorrect would leak the key.
        """
        await self.send_json({**event["payload"], "type": events.ANSWER_PROGRESS})

    async def result_revealed(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.RESULT_REVEALED})

    async def leaderboard_updated(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.LEADERBOARD_UPDATED})

    async def competition_results(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.COMPETITION_RESULTS})

    async def tick(self, event) -> None:
        # Group traffic carries its payload under "payload", like every other
        # event; a tick sent straight to the socket has no envelope.
        payload = event.get("payload", event)
        await self.send_json(
            {
                "type": events.TICK,
                "server_time": payload.get(
                    "server_time", timezone.now().isoformat()
                ),
                "seconds_remaining": payload.get("seconds_remaining"),
                "state": payload.get("state"),
                "question_number": payload.get("question_number"),
            }
        )

    # -- helpers -----------------------------------------------------------

    async def _send_state_sync(self) -> None:
        competition = await self._reload_competition()
        if competition is None:
            return
        connected = await self._connected_screen_count(competition.pk)
        await self.send_json(
            events.state_payload(
                competition,
                screen=self.screen,
                connected_screens=connected,
            )
        )

    async def _send_tick(self) -> None:
        competition = await self._reload_competition()
        now = timezone.now()
        payload = {
            "type": events.TICK,
            "server_time": now.isoformat(),
            "seconds_remaining": round(
                remaining_seconds(competition.current_question_ends_at, now), 3
            ),
            "state": competition.state,
            "question_number": competition.question_number,
        }
        await self.send_json(payload)

    async def _announce_presence(self) -> None:
        """Tell the teacher how many screens are attached."""
        if not self.competition_id:
            return
        count = await self._connected_screen_count(self.competition_id)
        await self.channel_layer.group_send(
            groups.teacher_group(self.competition_id),
            {
                "type": "presence_changed",
                "payload": {
                    "type": events.PRESENCE,
                    "competition_id": self.competition_id,
                    "connected_screens": count,
                },
            },
        )

    def _query_param(self, name: str) -> str | None:
        from urllib.parse import parse_qs

        raw = self.scope.get("query_string") or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        values = parse_qs(raw).get(name)
        return values[0] if values else None

    # -- database access ---------------------------------------------------

    async def _within_rate_limit(self) -> bool:
        """Whether this connection may present another Screen ID.

        Counted against the client address in the WebSocket handshake. The
        limiter touches the cache, which is not coroutine-safe, so it runs in a
        worker thread like every other database-backed call here.
        """
        return await self._screen_rate_limit(self._client_address())

    def _client_address(self) -> str:
        """The socket's peer address.

        Taken from the ASGI scope. ``X-Forwarded-For`` is honoured only when
        ``SCREEN_TRUST_FORWARDED_FOR`` is set, exactly as on the HTTP path in
        :mod:`screens.network`: honouring it unconditionally would let any client
        send a fresh value on every connection and reset its own counter, which
        would defeat the limit completely.
        """
        from django.conf import settings

        if getattr(settings, "SCREEN_TRUST_FORWARDED_FOR", False):
            headers = {k.lower(): v for k, v in (self.scope.get("headers") or [])}
            forwarded = headers.get(b"x-forwarded-for")
            if forwarded:
                candidate = forwarded.decode("utf-8", "replace").split(",")[0].strip()
                if candidate:
                    return candidate

        client = self.scope.get("client")
        if client and len(client) >= 2:
            return str(client[1])
        return "unknown"

    @database_sync_to_async
    def _screen_rate_limit(self, address: str) -> bool:
        from core.ratelimit import screen_identifier_allowed

        return screen_identifier_allowed(address)

    @database_sync_to_async
    def _load_screen(self, screen_id: str):
        from screens.models import InteractiveScreen

        return (
            InteractiveScreen.objects.active()
            .filter(screen_id=screen_id)
            .select_related("classroom")
            .first()
        )

    @database_sync_to_async
    def _active_competition(self):
        """The competition currently waiting for screens.

        Phase 4 runs one round at a time, so this is simply the newest started
        round that has not finished. Scoped to started rounds so that a screen
        connecting before the teacher presses start gets a clear refusal rather
        than a silent hang.
        """
        from competitions.models import Competition

        return (
            Competition.objects.filter(started_at__isnull=False, finished_at__isnull=True)
            .prefetch_related("classrooms")
            .order_by("-started_at")
            .first()
        )

    @database_sync_to_async
    def _classroom_included(self, competition_id: int, classroom_id: int) -> bool:
        from competitions.models import CompetitionClassroom

        return CompetitionClassroom.objects.filter(
            competition_id=competition_id, classroom_id=classroom_id
        ).exists()

    @database_sync_to_async
    def _reload_competition(self):
        from competitions.models import Competition

        if not self.competition_id:
            return None
        return (
            Competition.objects.filter(pk=self.competition_id)
            .select_related("current_question")
            .first()
        )

    @database_sync_to_async
    def _submit(
        self,
        competition_id: int,
        screen_id: int,
        classroom_id: int | None,
        selection: str,
    ) -> dict:
        """Record the answer in a worker thread and report what was stored.

        Returns only the fields the submitting screen is allowed to know. The
        score and correctness stay on the server until the reveal.
        """
        from django.utils import timezone

        from competitions.models import Competition
        from screens.models import InteractiveScreen
        from scoring.services import AnswerRejected, submit_answer

        competition = (
            Competition.objects.filter(pk=competition_id)
            .select_related("current_question", "scoring_rule")
            .prefetch_related("classrooms")
            .first()
        )
        if competition is None:
            raise AnswerRejected("missing_competition", "This competition has ended.")

        screen = InteractiveScreen.objects.filter(pk=screen_id).first()
        classroom = screen.classroom if screen else None

        answer = submit_answer(
            competition=competition,
            classroom=classroom,
            screen=screen,
            selection=selection,
        )

        return {
            "position": answer.position,
            "server_time": timezone.now().isoformat(),
            "response_time_seconds": answer.response_time_seconds,
        }

    async def _connected_screen_count(self, competition_id: int) -> int:
        """Count screens currently attached to this competition.

        Redis holds the definitive membership, so this is exact across several
        web processes rather than approximate per process.
        """
        from live.presence import count_group_members

        return await count_group_members(
            self.channel_layer, groups.screen_group(competition_id)
        )
