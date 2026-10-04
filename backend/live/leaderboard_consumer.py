"""The leaderboard display WebSocket consumer.

A classroom often shows the standings on its own display, and the person
operating that display is whoever is free - not necessarily the teacher running
the round. So this consumer is authorised exactly like the teacher consumer:
signed in, staff, and either the owner of the competition or an administrator.

That is a deliberate narrowing. The leaderboard is the one thing on display that
must never be wrong or forged, and it also carries the answer key after a reveal,
so it gets the strictest treatment rather than the loosest.

Read-only by construction: this consumer has no control handlers at all, so a
message sent to it is answered with an error and changes nothing.
"""

from __future__ import annotations

import json
import logging

from channels.db import database_sync_to_async
from django.utils import timezone

from competitions.models import Competition
from live import events, groups
from live.consumers_base import CLOSE_UNAUTHORISED, BaseLiveConsumer, error_payload
from live.services import CompetitionControlError, assert_can_control

logger = logging.getLogger(__name__)


class LeaderboardConsumer(BaseLiveConsumer):
    """A display showing the live standings for one competition."""

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
            assert_can_control(competition, self.user)
        except CompetitionControlError as error:
            await self.close(code=CLOSE_UNAUTHORISED)
            logger.info("Leaderboard WebSocket refused: %s", error.code)
            return

        await self.accept()
        self.competition = competition
        self.group = groups.leaderboard_group(competition.pk)
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
        await self._send_leaderboard()

    async def disconnect(self, code) -> None:
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)
        logger.info("Leaderboard WebSocket closed (code=%s).", code)

    async def receive(self, text_data: str = None, bytes_data: str = None) -> None:
        if not text_data:
            return

        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json(error_payload("bad_json", "Malformed message."))
            return

        kind = message.get("type") if isinstance(message, dict) else None
        if kind == "ping":
            await self.send_json(
                {
                    "type": events.TICK,
                    "server_time": timezone.now().isoformat(),
                }
            )
            return

        await self.send_json(
            error_payload("read_only", "This display cannot control a competition.")
        )

    # -- outbound events ----------------------------------------------------

    async def leaderboard_updated(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.LEADERBOARD_UPDATED})

    async def result_revealed(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.RESULT_REVEALED})

    async def competition_results(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.COMPETITION_RESULTS})

    async def competition_started(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.COMPETITION_STARTED})

    async def competition_finished(self, event) -> None:
        await self.send_json({**event["payload"], "type": events.COMPETITION_FINISHED})

    async def tick(self, event) -> None:
        payload = event.get("payload", event)
        await self.send_json({**payload, "type": events.TICK})

    async def presence_changed(self, event) -> None:
        return

    async def question_started(self, event) -> None:
        return

    async def answering_closed(self, event) -> None:
        return

    async def next_question(self, event) -> None:
        return

    # -- helpers ------------------------------------------------------------

    async def _send_leaderboard(self) -> None:
        from scoring.leaderboard import leaderboard_payload

        competition = await self._load_competition()
        if competition is None:
            return
        await self.send_json(
            {**leaderboard_payload(competition), "type": events.LEADERBOARD_UPDATED}
        )

    # -- database access ----------------------------------------------------

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
        return Competition.objects.filter(pk=self.competition_id).first()