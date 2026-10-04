"""URLs for the scoring display."""

from __future__ import annotations

from django.urls import path

from scoring.views import LeaderboardView

app_name = "scoring"

urlpatterns = [
    path(
        "competitions/<int:competition_id>/leaderboard/",
        LeaderboardView.as_view(),
        name="leaderboard",
    ),
]