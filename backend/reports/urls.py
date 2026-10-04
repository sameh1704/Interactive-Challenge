"""URLs for reports and CSV exports.

Mounted at ``/reports/``. Everything requires a signed-in member of staff; each
report additionally applies the permission rule of the thing it reports on, so a
teacher cannot reach another teacher's results by guessing a competition id.

The CSV routes are deliberately separate paths rather than a ``?format=csv``
parameter: a download should be a link an administrator can put in a document,
and query-string switches are easy to lose when a URL is copied.
"""

from __future__ import annotations

from django.urls import path

from reports import views

app_name = "reports"

urlpatterns = [
    path("", views.ReportIndexView.as_view(), name="index"),
    path(
        "competitions/<int:competition_id>/",
        views.CompetitionReportView.as_view(),
        name="competition",
    ),
    path(
        "competitions/<int:competition_id>/export.csv",
        views.CompetitionReportCsvView.as_view(),
        name="competition_csv",
    ),
    path(
        "classrooms/<int:classroom_id>/",
        views.ClassroomReportView.as_view(),
        name="classroom",
    ),
    path(
        "classrooms/<int:classroom_id>/export.csv",
        views.ClassroomReportCsvView.as_view(),
        name="classroom_csv",
    ),
    path(
        "tournaments/<int:tournament_id>/",
        views.TournamentReportView.as_view(),
        name="tournament",
    ),
    path(
        "tournaments/<int:tournament_id>/export.csv",
        views.TournamentReportCsvView.as_view(),
        name="tournament_csv",
    ),
    path(
        "questions/<int:question_id>/",
        views.QuestionReportView.as_view(),
        name="question",
    ),
]
