"""Admin interface for classrooms."""

from __future__ import annotations

from django.contrib import admin

from classrooms.models import Classroom
from core.admin import AdministratorAdminMixin


@admin.register(Classroom)
class ClassroomAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """Create and manage classrooms.

    Registered screens are editable here so that an administrator can move a
    screen between classrooms without leaving the classroom record. Screens are
    *not* deleted with a classroom: the foreign key is ``PROTECT``, so a
    classroom holding registered screens must have them reassigned or removed
    first. That is deliberate - a competition result must never lose the screen
    it was played on because a room was tidied up.
    """

    list_display = (
        "name",
        "grade",
        "section",
        "location_display",
        "assigned_teachers",
        "screen_count",
        "active",
    )
    list_filter = ("active", "grade", "building")
    search_fields = ("name", "grade", "section", "building", "room_number")
    ordering = ("name",)
    filter_horizontal = ("teachers",)
    list_editable = ("active",)
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("name", "grade", "section")}),
        (
            "Location",
            {
                "classes": ("collapse",),
                "fields": ("building", "floor", "room_number"),
            },
        ),
        (
            "Assignment",
            {
                "fields": ("teachers", "active"),
                "description": (
                    "Instructors assigned to this classroom. Interactive screens "
                    "are assigned from the Interactive screens page."
                ),
            },
        ),
        ("Audit", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Location")
    def location_display(self, obj: Classroom) -> str:
        return obj.location or "—"

    @admin.display(description="Teachers")
    def assigned_teachers(self, obj: Classroom) -> str:
        names = sorted(user.get_full_name() for user in obj.teachers.all())
        return ", ".join(names) or "—"

    @admin.display(description="Screens")
    def screen_count(self, obj: Classroom) -> int:
        return obj.screens.count()