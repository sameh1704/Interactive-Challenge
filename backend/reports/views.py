"""Report pages and CSV exports.

Every page here requires a signed-in member of staff. Authorisation for a
*particular* report reuses the rule that already governs that data - a teacher may
report on their own competition and an administrator on any - so a report cannot
become a way to read a result the live dashboard would refuse to show.

The report functions live in :mod:`reports.services` and the CSV builders here
only format what those return. The same function therefore serves the page and the
download, which is what makes "the CSV matches the screen" true by construction
rather than by maintenance.
"""

from __future__ import annotations

from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.generic import TemplateView

from accounts.permissions import is_staff_member
from classrooms.models import Classroom
from competitions.models import Competition
from live.services import CompetitionControlError, assert_can_control
from questions.models import Question
from reports import services
from reports.exports import csv_response, slugify_filename
from tournaments.models import Tournament


def _staff_required(view):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_staff_member(request.user):
            raise Http404("This account has no assigned role.")
        return view(self, request, *args, **kwargs)

    return dispatch


def _competition_for(user, competition_id) -> Competition:
    """A competition the viewer is allowed to report on.

    The same rule as the live dashboard and the leaderboard page, so a report is
    never a wider door than the pages around it.
    """
    competition = get_object_or_404(
        Competition.objects.prefetch_related("classrooms__classroom"), pk=competition_id
    )
    try:
        assert_can_control(competition, user)
    except CompetitionControlError:
        raise Http404 from None
    return competition


def _tournament_for(user, tournament_id) -> Tournament:
    tournament = get_object_or_404(
        Tournament.objects.select_related("created_by"), pk=tournament_id
    )
    from tournaments.services import TournamentError, assert_can_manage

    try:
        assert_can_manage(tournament, user)
    except TournamentError:
        raise Http404 from None
    return tournament


def _classroom_for(user, classroom_id) -> Classroom:
    """A classroom the viewer may report on.

    Administrators may report on any classroom; a teacher only on their own,
    matching ``ClassroomQuerySet.for_user``.
    """
    classroom = get_object_or_404(
        Classroom.objects.for_user(user).select_related(), pk=classroom_id
    )
    return classroom


class ReportIndexView(TemplateView):
    """A way in: pick what to report on."""

    template_name = "reports/index.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from accounts.permissions import is_administrator

        context.update(
            {
                "competitions": services.competition_options()[:25],
                "classrooms": Classroom.objects.for_user(self.request.user).order_by(
                    "name"
                )[:50],
                "tournaments": Tournament.objects.for_user(self.request.user).order_by(
                    "-created_at"
                )[:25],
                "is_administrator": is_administrator(self.request.user),
            }
        )
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class CompetitionReportView(TemplateView):
    """One competition: every classroom, every question, and the totals."""

    template_name = "reports/competition.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = _competition_for(self.request.user, self.kwargs["competition_id"])

        context.update(
            {
                "report": services.competition_report(competition),
                "competition": competition,
            }
        )
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class CompetitionReportCsvView(TemplateView):
    """The same report, as a download."""

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, competition_id, *args, **kwargs):
        competition = _competition_for(request.user, competition_id)
        report = services.competition_report(competition)

        rows = [
            [
                row["classroom"],
                row["correct"],
                row["wrong"],
                row["given"],
                "" if row["accuracy"] is None else f'{row["accuracy"]}%',
                row["score"],
                "" if row["average_response_seconds"] is None
                else row["average_response_seconds"],
            ]
            for row in report["classrooms"]
        ]

        return csv_response(
            slugify_filename("competition", competition.title) + ".csv",
            [
                "Classroom",
                "Correct",
                "Wrong",
                "Answers given",
                "Accuracy",
                "Final score",
                "Average response (s)",
            ],
            rows,
        )


class ClassroomReportView(TemplateView):
    """One classroom's record across every round it has played."""

    template_name = "reports/classroom.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        classroom = _classroom_for(self.request.user, self.kwargs["classroom_id"])

        context.update(
            {
                "classroom": classroom,
                "report": services.classroom_performance(classroom),
            }
        )
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class ClassroomReportCsvView(TemplateView):
    """The same classroom record, as a download."""

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, classroom_id, *args, **kwargs):
        classroom = _classroom_for(request.user, classroom_id)
        report = services.classroom_performance(classroom)

        rows = [
            [
                report["classroom"].display_name,
                report["competitions_played"],
                report["answers_given"],
                report["correct_answers"],
                report["wrong_answers"],
                "" if report["accuracy"] is None else f'{report["accuracy"]}%',
                report["total_score"],
                "" if report["average_score"] is None else report["average_score"],
            ]
        ]

        return csv_response(
            slugify_filename("classroom", classroom.name) + ".csv",
            [
                "Classroom",
                "Competitions played",
                "Answers given",
                "Correct",
                "Wrong",
                "Accuracy",
                "Total score",
                "Average score",
            ],
            rows,
        )


class TournamentReportView(TemplateView):
    """One championship: stages, participants, finalists and the final result."""

    template_name = "reports/tournament.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tournament = _tournament_for(self.request.user, self.kwargs["tournament_id"])

        context.update(
            {
                "tournament": tournament,
                "report": services.tournament_report(tournament),
            }
        )
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class TournamentReportCsvView(TemplateView):
    """The same championship report, as a download."""

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, tournament_id, *args, **kwargs):
        tournament = _tournament_for(request.user, tournament_id)
        report = services.tournament_report(tournament)

        # The first row names the tournament and season. A CSV that lands in a
        # folder on its own should say what it is, and a school keeping several
        # years of these needs the season on the file rather than only in a
        # filename.
        rows = [
            [tournament.name, tournament.season or "", "", ""],
        ]
        rows += [
            [
                row["stage"].order,
                row["stage"].display_name,
                row["stage"].get_status_display(),
                "; ".join(
                    f'{entry["competition"].title} ({entry["score"]} pts)'
                    for entry in _stage_winners(row)
                ),
            ]
            for row in report["stages"]
        ]

        for row in report["standings"]:
            rows.append(
                [
                    row["rank"],
                    "Final",
                    "Completed",
                    f'{row["classroom"]}: {row["score"]} pts',
                ]
            )

        return csv_response(
            slugify_filename("tournament", tournament.name) + ".csv",
            ["Tournament / Order", "Season / Stage", "Status", "Detail"],
            rows,
        )


def _stage_winners(stage_row: dict) -> list[dict]:
    """The leading classroom of each round in a stage, for the export."""
    winners = []
    for entry in stage_row["competitions"]:
        if not entry["classrooms"]:
            continue
        leader = entry["classrooms"][0]
        winners.append(
            {"competition": entry["competition"], "score": leader["score"]}
        )
    return winners


class QuestionReportView(TemplateView):
    """How one question has performed wherever it has been asked."""

    template_name = "reports/question.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        question = get_object_or_404(Question, pk=self.kwargs["question_id"])

        context.update(
            {
                "question": question,
                "report": services.question_statistics(question),
            }
        )
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


__all__ = [
    "ClassroomReportCsvView",
    "ClassroomReportView",
    "CompetitionReportCsvView",
    "CompetitionReportView",
    "QuestionReportView",
    "ReportIndexView",
    "TournamentReportCsvView",
    "TournamentReportView",
]
