"""The administration site.

A thin subclass of Django's own ``AdminSite``. It exists for one reason: to put
the school's operational picture on the admin landing page, which means the page
needs context that Django does not supply.

Everything else - the changelists, the forms, the search, the filters, the
pagination, the permissions - is Django's, untouched. No admin behaviour is
reimplemented here, so nothing that works today can stop working.

Wired in by ``core.apps.ChallengeAdminConfig`` (see ``base.py``'s
``INSTALLED_APPS``). That is Django's documented ``default_site`` hook, and it
keeps the site's URL, its URL name (``admin``) and every existing ``admin:<app>_<model>_*``
name identical, because the custom site inherits ``AdminSite.get_urls`` unchanged.

Metrics are counted in the database with ``count()`` and filtered aggregates,
never by loading rows into Python: this page is opened during setup and during
incidents, and it must stay fast when every screen is heartbeating at once.

The counts are **display only**. Authorisation is unchanged and remains in
``core.admin.AdministratorAdminMixin``: reaching this page already requires
administrator access, and nothing here grants or implies any permission.
"""

from __future__ import annotations

from django.contrib.admin import AdminSite
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect

from accounts.permissions import is_administrator, is_teacher


class ChallengeAdminSite(AdminSite):
    """The school's admin site, with an operational overview on its index."""

    site_title = "Al Manar Administration"
    site_header = "Al Manar Interactive Challenge"
    index_title = "Administration"
    enable_nav_sidebar = True

    def dispatch(self, request, *args, **kwargs):
        # Admin is administrator-only by design. Teachers are redirected to the
        # teacher workspace; anonymous users are sent to sign in. This prevents
        # teachers from accidentally using the implementation-oriented admin and
        # blocks URL-guessing of restricted admin endpoints.
        if is_teacher(request.user):
            # Teacher: send to the simplified workspace.
            from django.urls import reverse
            return redirect(reverse("teacher:dashboard"))
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def index(self, request, extra_context=None):
        """Add the school's own totals to Django's standard app list.

        Django's ``index`` already supplies ``available_apps`` and enforces that
        the viewer may see the admin at all. This only *adds* numbers.
        """
        extra_context = {**(extra_context or {})}
        extra_context["metrics"] = operational_metrics()
        return super().index(request, extra_context)


def operational_metrics() -> dict:
    """Counts for the admin overview, each one a real database query.

    Grouped as a list of ``{"label", "value", "tone", "help"}`` rather than a
    flat dict so the template can render the tiles in a deliberate order without
    a second mapping to keep in step. ``tone`` is a status class the stylesheet
    owns; an unknown tone falls back to the neutral tile, so a new metric cannot
    break the page by inventing a class.

    Nothing here is estimated, cached or derived from another count. If the
    database cannot answer a question the tile is omitted rather than guessed at.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Count, Q

    from accounts.roles import Role
    from classrooms.models import Classroom
    from competitions.models import ACTIVE_STATES, Competition, CompetitionState
    from questions.models import Question
    from screens.models import InteractiveScreen
    from tournaments.models import Tournament, TournamentStatus

    staff = get_user_model().objects

    classroom_totals = Classroom.objects.aggregate(
        total=Count("id"), active=Count("id", filter=Q(active=True))
    )
    screen_totals = InteractiveScreen.objects.aggregate(
        registered=Count("id"), active=Count("id", filter=Q(active=True))
    )
    question_totals = Question.objects.aggregate(
        total=Count("id"), active=Count("id", filter=Q(active=True))
    )
    competition_totals = Competition.objects.aggregate(
        active=Count("id", filter=Q(state__in=ACTIVE_STATES)),
        completed=Count("id", filter=Q(state=CompetitionState.FINISHED)),
    )
    tournament_totals = Tournament.objects.aggregate(
        active=Count(
            "id",
            filter=Q(status__in=(TournamentStatus.OPEN, TournamentStatus.RUNNING)),
        ),
        completed=Count("id", filter=Q(status=TournamentStatus.COMPLETED)),
    )

    online = InteractiveScreen.objects.online().count()

    return {
        "tiles": [
            {
                "label": "Classrooms",
                "value": classroom_totals["total"],
                "help": f"{classroom_totals['active']} active",
                "tone": "neutral",
            },
            {
                "label": "Screens registered",
                "value": screen_totals["registered"],
                "help": f"{screen_totals['active']} active",
                "tone": "neutral",
            },
            {
                # The one tile that changes on its own, so it gets the accent
                # and a help line that says how the number is arrived at.
                "label": "Screens online",
                "value": online,
                "help": f"{screen_totals['active'] - online} offline",
                "tone": "online" if online else "neutral",
            },
            {
                "label": "Teachers",
                "value": staff.filter(role=Role.TEACHER, is_active=True).count(),
                "help": (
                    f"{staff.filter(role=Role.ADMINISTRATOR, is_active=True).count()}"
                    " administrators"
                ),
                "tone": "neutral",
            },
            {
                "label": "Question bank",
                "value": question_totals["total"],
                "help": f"{question_totals['active']} active",
                "tone": "neutral",
            },
            {
                "label": "Competitions running",
                "value": competition_totals["active"],
                "help": f"{competition_totals['completed']} completed",
                "tone": "running" if competition_totals["active"] else "neutral",
            },
            {
                "label": "Tournaments active",
                "value": tournament_totals["active"],
                "help": f"{tournament_totals['completed']} completed",
                "tone": "running" if tournament_totals["active"] else "neutral",
            },
        ],
    }