"""URLs for the teacher workspace.

Mounted at ``/teacher/``. Every route requires a signed-in, active member of
staff. Teachers see only what they are authorised to see; administrators see
the full picture through the same views, which is how the workspace doubles
as the operational dashboard for both roles.
"""

from __future__ import annotations

from django.urls import path

from teacher import views
from teacher.tournament_wizard import TournamentWizardView
from teacher.wizard import CompetitionWizardView, ResumeCompetitionWizardView

app_name = "teacher"

urlpatterns = [
    path("competitions/<int:pk>/resume/", ResumeCompetitionWizardView.as_view(), name="resume_competition"),
    path("", views.TeacherDashboardView.as_view(), name="dashboard"),
    path("competitions/", views.CompetitionListView.as_view(), name="competitions"),
    path(
        "competitions/create/",
        CompetitionWizardView.as_view(),
        name="create_competition",
    ),
    path("competitions/<int:pk>/", views.CompetitionDetailView.as_view(), name="competition_detail"),
    path(
        "competitions/<int:pk>/start/",
        views.StartCompetitionView.as_view(),
        name="start_competition",
    ),
    path(
        "competitions/<int:pk>/plan/",
        views.PlanCompetitionView.as_view(),
        name="plan_competition",
    ),
    path("questions/", views.QuestionBankView.as_view(), name="questions"),
    path("questions/create/", views.QuestionCreateView.as_view(), name="create_question"),
    path(
        "questions/<int:pk>/",
        views.QuestionDetailView.as_view(),
        name="question_detail",
    ),
    path(
        "questions/<int:pk>/edit/",
        views.QuestionEditView.as_view(),
        name="edit_question",
    ),
    path("tournaments/", views.TournamentListView.as_view(), name="tournaments"),
    path("tournaments/create/", TournamentWizardView.as_view(), name="create_tournament"),
    path("tournaments/<int:pk>/", views.TournamentDetailView.as_view(), name="tournament_detail"),
    path("reports/", views.ReportView.as_view(), name="reports"),
    path("hall-of-fame/", views.HallOfFameView.as_view(), name="hall_of_fame"),
    path("help/", views.HelpView.as_view(), name="help"),
    path("set_language/", views.set_language, name="set_language"),
]