"""The tournament pages: list, dashboard, actions and the Hall of Fame.

Authorisation
-------------
Every page here requires a signed-in member of staff. Every *action* additionally
requires :func:`tournaments.services.assert_can_manage`, so the same rule the live
engine uses for a round governs who may change a championship.

State changes are server-side
-----------------------------
The browser tells the server which action it wants and which ids it means. It never
tells the server what the outcome is. A form cannot name a winner, a score, a
standing or a new status: each action is a call into the service layer, which
re-reads the database and decides. An administrator who clicks "process
advancement" on a stage that is not complete gets a refusal, not an advancement.

That is also why the buttons are merely a convenience. Which actions are offered
in the template is a usability decision; the guarantee is that offering the wrong
one is harmless.
"""

from __future__ import annotations

import datetime
import logging

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import TemplateView

from accounts.permissions import is_administrator, is_staff_member
from competitions.models import Competition
from tournaments import services
from tournaments.models import (
    AdvancementStatus,
    Tournament,
    TournamentStage,
    TournamentStatus,
)

logger = logging.getLogger(__name__)


def _staff_required(view):
    """Send anonymous visitors to sign in and refuse anyone without a role."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_staff_member(request.user):
            raise Http404("This account has no assigned role.")
        return view(self, request, *args, **kwargs)

    return dispatch


class TournamentListView(TemplateView):
    """Every tournament this member of staff may open."""

    template_name = "tournaments/list.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        tournaments = (
            Tournament.objects.for_user(self.request.user)
            .select_related("created_by")
            .prefetch_related("stages", "participants__classroom")
        )

        rows = []
        for tournament in tournaments:
            stages = list(tournament.stages.all())
            rows.append(
                {
                    "tournament": tournament,
                    "stage_count": len(stages),
                    "participant_count": tournament.participants.count(),
                    "current_stage": tournament.current_stage,
                }
            )

        context.update(
            {
                "rows": rows,
                "is_administrator": is_administrator(self.request.user),
                "statuses": TournamentStatus,
            }
        )
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class TournamentCreateView(TemplateView):
    """Create a tournament.

    Always in ``draft``: nothing may be attached to it, so a championship cannot
    be half-built and then run by accident. Opening it is a separate, deliberate
    action.
    """

    template_name = "tournaments/form.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "values": {
                    "name": "",
                    "description": "",
                    "subject": "",
                    "grade": "",
                    "season": str(datetime.date.today().year),
                },
                "errors": {},
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        values = {
            "name": request.POST.get("name", "").strip(),
            "description": request.POST.get("description", "").strip(),
            "subject": request.POST.get("subject", "").strip(),
            "grade": request.POST.get("grade", "").strip(),
            "season": request.POST.get("season", "").strip(),
        }

        try:
            tournament = services.create_tournament(
                created_by=request.user, **values
            )
        except services.TournamentError as error:
            return render(
                request,
                self.template_name,
                {"values": values, "errors": {"name": error.message}},
                status=400,
            )

        messages.success(request, f'Created "{tournament.name}" as a draft.')
        return redirect("tournaments:detail", pk=tournament.pk)

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class TournamentDetailView(TemplateView):
    """One championship: its stages, its classrooms and what may be done next."""

    template_name = "tournaments/detail.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_tournament(self) -> Tournament:
        cached = getattr(self, "_tournament", None)
        if cached is not None:
            return cached

        # `for_user` rather than a plain lookup: an unauthorised member of staff
        # is refused the same way the live dashboard refuses them, so they cannot
        # discover a championship exists by seeing a 403 instead of a 404.
        tournament = get_object_or_404(
            Tournament.objects.for_user(self.request.user).select_related(
                "created_by"
            ),
            pk=self.kwargs["pk"],
        )
        self._tournament = tournament
        return tournament

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tournament = self.get_tournament()

        participants = list(
            tournament.participants.select_related("classroom").order_by(
                "classroom__name"
            )
        )

        stages = []
        for stage in tournament.ordered_stages:
            advancement = getattr(stage, "advancement", None)
            standings = (
                services.stage_standings(stage) if stage.is_completed else []
            )
            stages.append(
                {
                    "stage": stage,
                    "competitions": list(stage.attached_competitions),
                    "advancement": advancement,
                    "standings": standings,
                    "standings_with_ranks": [
                        _standing_row(standing, advancement)
                        for standing in standings
                    ],
                    "is_tie": bool(advancement and advancement.is_tie),
                    # The tied classrooms with their ids, so the resolution form
                    # can post a classroom rather than a name.
                    "tied_classrooms": (
                        [
                            {"pk": participant.classroom_id, "name": participant.classroom.display_name}
                            for participant in participants
                            if participant.classroom_id in advancement.tied_classroom_ids
                        ]
                        if advancement and advancement.is_tie
                        else []
                    ),
                }
            )

        participants = list(
            tournament.participants.select_related("classroom").order_by(
                "classroom__name"
            )
        )

        # Classrooms that could still be entered. Everything the school has, minus
        # those already in the championship, so the form cannot offer a duplicate.
        from classrooms.models import Classroom

        taken = {participant.classroom_id for participant in participants}
        available = [
            classroom
            for classroom in Classroom.objects.filter(active=True).order_by("name")
            if classroom.pk not in taken
        ]

        context.update(
            {
                "tournament": tournament,
                "stages": stages,
                "participants": participants,
                "available_classrooms": available,
                "available_competitions": Competition.objects.filter(
                    state="finished", result__isnull=False
                )
                .order_by("-finished_at")[:25],
                "record": getattr(tournament, "record", None),
                "history": services.history_for(tournament),
                "is_administrator": is_administrator(self.request.user),
                "can_manage": self._can_manage(tournament),
                "is_draft": tournament.status == TournamentStatus.DRAFT,
                "is_open": tournament.status == TournamentStatus.OPEN,
                "is_cancelled": tournament.is_cancelled,
                "is_completed": tournament.is_completed,
                "report_url": reverse(
                    "reports:tournament", kwargs={"tournament_id": tournament.pk}
                ),
            }
        )
        return context

    def _can_manage(self, tournament: Tournament) -> bool:
        try:
            services.assert_can_manage(tournament, self.request.user)
        except services.TournamentError:
            return False
        return True

    def get_login_url(self) -> str:
        return reverse("accounts:login")


def _standing_row(standing, advancement) -> dict:
    """One row of a stage's standings, for the table.

    Whether a classroom advanced is read from the stored entry when there is one,
    because that is the recorded decision; before advancement is processed the row
    simply has nothing to report.
    """
    entry = None
    if advancement is not None:
        entry = advancement.entries.filter(
            participant_id=standing.participant_id
        ).first()

    return {
        "standing": standing,
        "rank": standing.rank,
        "advanced": entry.advanced if entry else None,
        "tied": entry.tied if entry else None,
    }


class TournamentActionView(TemplateView):
    """Every state change a tournament can undergo, in one guarded endpoint.

    One endpoint rather than seven so that authorisation, CSRF and error handling
    are written once. The action name is taken from the form; what it *means* is
    decided by the service layer, never by the request.
    """

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, pk, *args, **kwargs):
        tournament = self._manageable(pk)
        action = request.POST.get("action", "")

        handler = {
            "add_participant": self._add_participant,
            "open": self._open,
            "cancel": self._cancel,
            "create_stage": self._create_stage,
            "attach_competition": self._attach_competition,
            "complete_stage": self._complete_stage,
            "process_advancement": self._process_advancement,
            "resolve_tie": self._resolve_tie,
            "finalize": self._finalize,
        }.get(action)

        if handler is None:
            messages.error(request, f"Unsupported action: {action!r}.")
            return self._back(tournament)

        try:
            handler(request, tournament)
        except services.TournamentError as error:
            # A refusal is a normal outcome, not a crash: it is what a stale page
            # or a premature click produces, and the reason is shown to staff.
            messages.error(request, error.message)
            logger.info(
                "Tournament action %s refused for %s: %s",
                action,
                request.user,
                error.code,
            )

        return self._back(tournament)

    def _manageable(self, pk) -> Tournament:
        tournament = get_object_or_404(Tournament, pk=pk)
        try:
            services.assert_can_manage(tournament, self.request.user)
        except services.TournamentError:
            # The same 404 the page gives, for the same reason: an unauthorised
            # member of staff learns nothing from a 403 that a 404 would not
            # have revealed. Answering the POST differently from the GET would
            # make this endpoint a way to probe which tournaments exist.
            raise Http404 from None
        return tournament

    @staticmethod
    def _back(tournament: Tournament):
        return redirect("tournaments:detail", pk=tournament.pk)

    # -- actions ---------------------------------------------------------

    def _add_participant(self, request, tournament) -> None:
        from classrooms.models import Classroom

        classroom = get_object_or_404(
            Classroom, pk=request.POST.get("classroom")
        )
        services.add_participant(tournament, classroom)
        messages.success(request, f"Added {classroom.display_name}.")

    def _open(self, request, tournament) -> None:
        services.open_tournament(tournament)
        messages.success(request, "Tournament opened.")

    def _cancel(self, request, tournament) -> None:
        services.cancel_tournament(tournament)
        messages.success(request, "Tournament cancelled.")

    def _create_stage(self, request, tournament) -> None:
        advancing = request.POST.get("advancing_count", "").strip()
        stage = services.create_stage(
            tournament,
            request.POST.get("stage_type", ""),
            name=request.POST.get("name", ""),
            # Blank means "advance everyone", which is what a final does.
            advancing_count=int(advancing) if advancing else None,
        )
        messages.success(request, f"Added the {stage.display_name} stage.")

    def _attach_competition(self, request, tournament) -> None:
        stage = self._stage(request, tournament)
        competition = get_object_or_404(
            Competition, pk=request.POST.get("competition")
        )
        services.attach_competition(stage, competition)
        messages.success(request, f'Assigned "{competition.title}" to {stage.display_name}.')

    def _complete_stage(self, request, tournament) -> None:
        stage = self._stage(request, tournament)
        services.complete_stage(stage)
        messages.success(request, f"{stage.display_name} is complete.")

    def _process_advancement(self, request, tournament) -> None:
        stage = self._stage(request, tournament)
        advancement = services.process_advancement(stage, user=request.user)

        if advancement.status == AdvancementStatus.TIE:
            messages.warning(
                request,
                "Two or more classrooms are level on the cut-off. An "
                "administrator must choose who advances.",
            )
        else:
            names = ", ".join(
                entry.classroom_name
                for entry in advancement.entries.filter(advanced=True).select_related(
                    "participant__classroom"
                )
            )
            messages.success(request, f"Advanced: {names or 'nobody'}.")

    def _resolve_tie(self, request, tournament) -> None:
        stage = self._stage(request, tournament)
        chosen = [
            int(value)
            for value in request.POST.getlist("classroom_ids")
            if value.strip().isdigit()
        ]
        services.resolve_tie(stage, chosen, user=request.user)
        messages.success(request, "Tie resolved.")

    def _finalize(self, request, tournament) -> None:
        record = services.finalize_tournament(tournament)
        winner = record.winner_name or "no champion"
        messages.success(request, f"Tournament finalized. Champion: {winner}.")

    @staticmethod
    def _stage(request, tournament) -> TournamentStage:
        stage = get_object_or_404(
            TournamentStage, pk=request.POST.get("stage"), tournament=tournament
        )
        return stage


class HallOfFameView(TemplateView):
    """Completed tournaments, most recent first.

    Read-only and derived from the frozen
    :class:`tournaments.models.TournamentRecord` rows, so it is history rather
    than a live calculation and nothing on this page can be edited. An
    administrator who needs to correct a school record does it in the Django
    admin, deliberately and visibly, rather than here.

    Requires a signed-in member of staff, matching the leaderboard display: an
    interactive screen has no credentials and gets no access at all.
    """

    template_name = "tournaments/hall_of_fame.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        records = services.hall_of_fame()

        rows = []
        for record in records:
            rows.append(
                {
                    "record": record,
                    "standings": record.rows,
                    "participants": record.participant_names,
                }
            )

        context.update({"rows": rows, "is_administrator": is_administrator(self.request.user)})
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


__all__ = [
    "HallOfFameView",
    "TournamentActionView",
    "TournamentCreateView",
    "TournamentDetailView",
    "TournamentListView",
]
