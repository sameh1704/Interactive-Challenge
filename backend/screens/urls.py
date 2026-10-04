"""Screen-facing URLs.

Mounted at ``/screen/`` and reachable without signing in, because an interactive
screen has no operator. See :mod:`screens.views` for the security model.
"""

from __future__ import annotations

from django.urls import path

from screens import views

app_name = "screens"

urlpatterns = [
    path("", views.screen_status, name="status"),
    path("heartbeat/", views.screen_heartbeat, name="heartbeat"),
]