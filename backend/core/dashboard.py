"""Role-aware dashboards.

Lives in ``core`` because it aggregates across ``accounts``, ``classrooms`` and
``screens`` rather than owning any domain data of its own.

The statistics are computed with database-side aggregation. Counting rows in
Python would load every classroom and every screen into memory on every page
view, which is exactly the kind of thing that falls over during a live
competition when every screen is refreshing at once.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.db.models import Count, Q
from django.shortcuts import render
from django.urls import reverse
from django.views.generic import TemplateView

from accounts.permissions import is_administrator
from accounts.roles import Role
from classrooms.models import Classroom
from screens.models import InteractiveScreen


class DashboardView(TemplateView):
    """Landing page after signing in, tailored to the viewer's role."""

    template_name = "core/dashboard.html"

    def dispatch(self, request, *args, **kwargs):
        # An anonymous visitor has no dashboard; send them to sign in.
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_active:
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context["role"] = user.role
        context["role_display"] = user.get_role_display()
        context["is_administrator"] = is_administrator(user)

        if context["is_administrator"]:
            context.update(self._administrator_context())
        else:
            context.update(self._teacher_context(user))

        return context

    def _administrator_context(self) -> dict:
        """Whole-school totals plus the current screen picture."""
        classroom_stats = Classroom.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(active=True)),
        )
        screen_stats = InteractiveScreen.objects.aggregate(
            registered=Count("id"),
            active=Count("id", filter=Q(active=True)),
        )
        online_total = InteractiveScreen.objects.online().count()

        staff = get_user_model().objects
        teacher_count = staff.filter(role=Role.TEACHER, is_active=True).count()
        administrator_count = staff.filter(
            role=Role.ADMINISTRATOR, is_active=True
        ).count()

        screens = (
            InteractiveScreen.objects.select_related("classroom")
            .order_by("classroom__name", "name")
        )

        return {
            "stats": {
                "classrooms_total": classroom_stats["total"],
                "classrooms_active": classroom_stats["active"],
                "screens_registered": screen_stats["registered"],
                "screens_active": screen_stats["active"],
                "screens_online": online_total,
                "screens_offline": screen_stats["active"] - online_total,
                "teachers": teacher_count,
                "administrators": administrator_count,
            },
            "screens": screens,
        }

    def _teacher_context(self, user) -> dict:
        """Only the classrooms this teacher is assigned to."""
        classrooms = (
            Classroom.objects.for_user(user)
            .select_related()
            .prefetch_related("screens")
            .order_by("name")
        )

        classroom_list = list(classrooms)

        return {
            "classrooms": classroom_list,
            "stats": {
                "classrooms_total": len(classroom_list),
                "screens_total": InteractiveScreen.objects.filter(
                    classroom__in=classroom_list
                ).count(),
                "screens_online": InteractiveScreen.objects.online()
                .filter(classroom__in=classroom_list)
                .count(),
            },
        }

    def get_login_url(self) -> str:
        return reverse("accounts:login")