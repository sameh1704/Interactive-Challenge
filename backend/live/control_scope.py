"""Recheck long-lived staff sockets after role changes or ownership revocation."""
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from competitions.models import Competition
from live.consumers_base import CLOSE_UNAUTHORISED
from live.services import assert_can_control, CompetitionControlError


class ControlScopeMixin:
    @database_sync_to_async
    def _current_controller(self):
        user = get_user_model().objects.filter(pk=getattr(self.user, 'pk', None)).first()
        competition = Competition.objects.filter(pk=self.competition_id).first()
        if competition is None:
            return None
        try:
            assert_can_control(competition, user)
        except CompetitionControlError:
            return None
        return user

    async def receive(self, text_data=None, bytes_data=None):
        user = await self._current_controller()
        if user is None:
            await self.close(code=CLOSE_UNAUTHORISED)
            return
        self.user = user
        await self.receive_authorized(text_data=text_data, bytes_data=bytes_data)

    async def send_json(self, content, close=False):
        user = await self._current_controller()
        if user is None:
            await self.close(code=CLOSE_UNAUTHORISED)
            return
        self.user = user
        await super().send_json(content, close=close)
