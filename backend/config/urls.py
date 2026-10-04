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

Administrator-only surface:

* ``/admin/``      - Django admin, for teachers, classrooms and screens
"""

from django.contrib import admin
from django.urls import include, path

from core.dashboard import DashboardView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("screen/", include("screens.urls")),
    path("live/", include("live.urls")),
    path("scoring/", include("scoring.urls")),
    path("tournaments/", include("tournaments.urls")),
    path("reports/", include("reports.urls")),
    path("", include("core.urls")),
]

admin.site.site_header = "Al Manar Interactive Challenge"
admin.site.site_title = "Al Manar Interactive Challenge"
admin.site.index_title = "School administration"