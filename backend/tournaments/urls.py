"""URLs for the tournament pages.

Mounted at ``/tournaments/``. Every route requires a signed-in member of staff;
the actions additionally require ownership or the administrator role, which the
view checks through :func:`tournaments.services.assert_can_manage`.

An interactive screen has no credentials and reaches none of this, which is the
same boundary the live screen page draws.
"""

from __future__ import annotations

from django.urls import path

from tournaments import views

app_name = "tournaments"

urlpatterns = [
    path("", views.TournamentListView.as_view(), name="list"),
    path("new/", views.TournamentCreateView.as_view(), name="create"),
    # Before the numbered routes, so "hall-of-fame" is not read as a pk.
    path("hall-of-fame/", views.HallOfFameView.as_view(), name="hall_of_fame"),
    path("<int:pk>/", views.TournamentDetailView.as_view(), name="detail"),
    path("<int:pk>/actions/", views.TournamentActionView.as_view(), name="action"),
]
