"""WebSocket routing table.

Two endpoints, one per audience:

``/ws/live/screen/``
    Classroom screens. Unauthenticated at the transport level because a screen
    has no session; it presents its Screen ID as a query parameter and as an
    ``identify`` message. That makes the Screen ID a bearer credential, and it is
    validated exactly as strictly as the Phase 2 HTTP endpoints.

``/ws/live/teacher/<competition_id>/``
    Teacher dashboards. Authenticated by the Django session cookie supplied by
    ``AuthMiddlewareStack``, and then authorised against the competition's owner.

``/ws/live/leaderboard/<competition_id>/``
    A leaderboard display. Authenticated and authorised exactly like the teacher
    socket, because the standings it shows also carry the answer key after a
    reveal. Read-only: it has no control handlers at all.

The screen endpoint deliberately carries no competition id in its path. A screen
must not be able to nominate which competition it joins; the server decides that
from its classroom.
"""

from __future__ import annotations

from django.urls import path

from live.consumers import ScreenConsumer
from live.leaderboard_consumer import LeaderboardConsumer
from live.teacher_consumer import TeacherConsumer

websocket_urlpatterns = [
    path("ws/live/screen/", ScreenConsumer.as_asgi()),
    path(
        "ws/live/teacher/<int:competition_id>/",
        TeacherConsumer.as_asgi(),
    ),
    path(
        "ws/live/leaderboard/<int:competition_id>/",
        LeaderboardConsumer.as_asgi(),
    ),
]

__all__ = ["websocket_urlpatterns"]
