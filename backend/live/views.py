"""The two live pages: the classroom screen, and the teacher's live dashboard.

Both are server-rendered first and then kept current over the WebSocket, for the
same reason the leaderboard is: a display opened a second before a question starts
must show the right thing immediately rather than an empty shell waiting for a
socket.

The two audiences are kept strictly apart
------------------------------------------
:class:`LiveScreenView` is unauthenticated, because an interactive screen has no
operator and no credentials. Its only credential is the Screen ID in its URL,
which is validated exactly as strictly as the Phase 2 endpoints. It is given
nothing else: no competition list, no other classrooms, no controls, and no way
to reach the teacher socket.

:class:`TeacherLiveDashboardView` requires a signed-in member of staff *and*
ownership of the competition, reusing :func:`live.services.assert_can_control` -
the same rule the teacher WebSocket enforces, so the page and the socket cannot
disagree about who may drive the round.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.generic import TemplateView

from competitions.models import Competition
from live.services import CompetitionControlError, assert_can_control
from screens.models import InteractiveScreen

logger = logging.getLogger(__name__)


def control_state_for(competition: Competition) -> str:
    """The state the dashboard's controls are resolved against.

    ``Competition.state`` defaults to ``waiting``, which describes a round that
    has started and is between questions just as much as one that has never
    started. The controls need to tell those apart, because "Start competition"
    is only meaningful before the first start and "Start question" only after it.

    Returns ``not_started`` before the round begins and the stored state
    otherwise. :mod:`live.static.live.js.teacher` rebuilds the same value from a
    ``state_sync`` payload, so the page and the socket agree.
    """
    if competition.started_at is None:
        return "not_started"
    return competition.state


def normalise_screen_id(raw: str | None) -> str:
    """Fold a Screen ID to its canonical form for lookup.

    An operator types this by hand and a touchscreen keyboard may present it in
    any case, so ``am-7kq4xb`` and ``AM-7KQ4XB`` must find the same screen. The
    database already rejects two IDs differing only by case, so upper-casing here
    cannot merge two different screens.
    """
    return (raw or "").strip().upper()


class LiveScreenView(TemplateView):
    """The page an interactive screen runs during a competition.

    Reachable at ``/live/screen/?screen_id=AM-7KQ4XB``. Renders the same
    "not registered" page, with the same 404, for an unknown and a retired screen
    so this endpoint cannot be used to enumerate registered Screen IDs.
    """

    template_name = "live/screen.html"

    def get(self, request, *args, **kwargs):
        # The Screen ID is the only credential a screen has, so presenting one is
        # rate limited per client address here as well as on the status page.
        from core.ratelimit import screen_identifier_allowed
        from screens.network import client_ip

        if not screen_identifier_allowed(client_ip(request)):
            # The same unregistered page an unknown Screen ID renders, so the
            # caller still learns nothing about which IDs exist. screens.views
            # builds this same response, including Retry-After.
            from screens.views import throttled

            return throttled(request)

        screen_id = normalise_screen_id(request.GET.get("screen_id"))

        screen = (
            InteractiveScreen.objects.active()
            .select_related("classroom")
            .filter(screen_id=screen_id)
            .first()
        )

        if screen is None:
            logger.info("Live screen requested for an unregistered Screen ID.")
            return render(
                request,
                "screens/unregistered.html",
                {"screen_id": screen_id},
                status=404,
            )

        return render(
            request,
            self.template_name,
            {
                "screen": screen,
                "classroom": screen.classroom,
                # Only what this screen needs to render itself. Deliberately not
                # a competition id: the server decides which round a screen joins.
                "websocket_path": "/ws/live/screen/",
                "heartbeat_interval_seconds": settings.SCREEN_HEARTBEAT_INTERVAL_SECONDS,
            },
        )


class TeacherLiveDashboardView(TemplateView):
    """The teacher's controls for one running competition."""

    template_name = "live/teacher.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def get_competition(self) -> Competition:
        cached = getattr(self, "_competition", None)
        if cached is not None:
            return cached

        competition = get_object_or_404(
            Competition.objects.prefetch_related(
                "classrooms__classroom", "questions__question"
            ),
            pk=self.kwargs["competition_id"],
        )

        try:
            assert_can_control(competition, self.request.user)
        except CompetitionControlError:
            # A 404 rather than a 403: whether a competition exists at all is
            # not something an unauthorised user is entitled to learn. Same
            # reasoning as the leaderboard page.
            raise Http404 from None

        self._competition = competition
        return competition

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = self.get_competition()

        participating = [
            entry.classroom
            for entry in competition.classrooms.select_related("classroom").order_by(
                "classroom__name"
            )
        ]

        context.update(
            {
                "competition": competition,
                "participating_classrooms": participating,
                "state": competition.state,
                "state_display": competition.get_state_display(),
                # A round that has never been started has `state == waiting` but
                # no `started_at`, and the dashboard needs to tell those apart:
                # "Start competition" is only meaningful before the first start,
                # while "Start question" only after it. The same distinction is
                # reconstructed from `state_sync`, which carries `started_at`.
                "control_state": control_state_for(competition),
                "websocket_path": f"/ws/live/teacher/{competition.pk}/",
                "leaderboard_path": reverse(
                    "scoring:leaderboard", kwargs={"competition_id": competition.pk}
                ),
                "questions": list(
                    competition.questions.select_related("question").order_by("position")
                ),
            }
        )
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


__all__ = [
    "LiveScreenView",
    "TeacherLiveDashboardView",
    "control_state_for",
    "normalise_screen_id",
]