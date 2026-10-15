"""Root URL configuration.

Public surface:

* ``/``            - landing page
* ``/health/``     - health check, no authentication
* ``/screen/``     - screen registration and heartbeat, no authentication
* ``/live/``       - live classroom screen and teacher dashboard
* ``/dashboard/``  - signed-in staff dashboard
* ``/scoring/``    - leaderboard display, signed-in staff only
* ``/tournaments/`` - championships, stages, advancement and history
* ``/reports/``    - competition, classroom, question and tournament reports
* ``/accounts/``   - sign in / sign out

Teacher surface:

* ``/teacher/``    - simplified teacher workspace
* ``/workspace/``  - alias for the teacher workspace

Administrator-only surface:

* ``/admin/``      - Django admin, for teachers, classrooms and screens
"""

from django.contrib import admin
from django.urls import include, path
from django.utils.translation import gettext_lazy as _

from core.dashboard import DashboardView

urlpatterns = [
    path("deputy/", include("accounts.stage_urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("admin/", admin.site.urls),
    path("teacher/", include("teacher.urls")),
    path("workspace/", include("teacher.urls", namespace="workspace")),
    path("accounts/", include("accounts.urls")),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("screen/", include("screens.urls")),
    path("live/", include("live.urls")),
    path("scoring/", include("scoring.urls")),
    path("tournaments/", include("tournaments.urls")),
    path("reports/", include("reports.urls")),
    path("", include("core.urls")),
]

admin.site.site_header = _("Al Manar Interactive Challenge")
admin.site.site_title = _("Al Manar Interactive Challenge")
admin.site.index_title = _("School administration")
