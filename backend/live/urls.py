"""URLs for the live pages.

Mounted at ``/live/``. The screen page is reachable without signing in, because a
screen has no operator; the teacher dashboard is not, and authorises per
competition on top of requiring a session.
"""

from __future__ import annotations

from django.urls import path

from live import views

app_name = "live"

urlpatterns = [
    path("screen/", views.LiveScreenView.as_view(), name="screen"),
    path(
        "competitions/<int:competition_id>/",
        views.TeacherLiveDashboardView.as_view(),
        name="teacher_dashboard",
    ),
]