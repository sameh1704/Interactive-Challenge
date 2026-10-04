"""The leaderboard display.

A page, not a consumer-only endpoint: the standings have to render even if the
WebSocket has not connected yet, so a display left open across a round never
shows an empty board. The socket then keeps it current.

Authorisation matches the leaderboard WebSocket exactly - signed in, staff, and
the owner or an administrator - because the same data is on the wire in both
places. Letting one be open while the other is closed would make the strictness
of the socket pointless.
"""

from __future__ import annotations

from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.generic import TemplateView

from accounts.permissions import is_staff_member  # noqa: F401 - re-exported for tests
from competitions.models import Competition
from live.services import CompetitionControlError, assert_can_control
from scoring.leaderboard import leaderboard_rows


class LeaderboardView(TemplateView):
    """Live standings for one competition."""

    template_name = "scoring/leaderboard.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def get_competition(self) -> Competition:
        if getattr(self, "_competition", None) is not None:
            return self._competition

        competition = get_object_or_404(
            Competition.objects.prefetch_related("classrooms__classroom"),
            pk=self.kwargs["competition_id"],
        )

        # Reuse the live engine's rule so the page and the WebSocket cannot
        # disagree about who may see a given competition.
        try:
            assert_can_control(competition, self.request.user)
        except CompetitionControlError:
            # A 404 rather than a 403: whether a competition exists at all is
            # not something an unauthorised user is entitled to learn.
            raise Http404 from None

        self._competition = competition
        return competition

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = self.get_competition()

        context.update(
            {
                "competition": competition,
                "rows": leaderboard_rows(competition),
                "state": competition.state,
                "state_display": competition.get_state_display(),
                "websocket_path": f"/ws/live/leaderboard/{competition.pk}/",
                "result": getattr(competition, "result", None),
            }
        )
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


__all__ = ["LeaderboardView", "is_staff_member"]