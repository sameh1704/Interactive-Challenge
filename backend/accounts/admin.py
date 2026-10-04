"""Admin interface for school staff accounts.

Reached through the Django admin site, which only administrators can enter,
because ``User.is_staff`` is derived from the role.
"""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group

from accounts.models import User
from core.admin import AdministratorAdminMixin


@admin.register(User)
class UserAdmin(AdministratorAdminMixin, BaseUserAdmin):
    """Create and manage teachers and administrators."""

    list_display = ("username", "full_name", "role", "is_active", "date_joined")
    list_filter = ("role", "is_active", "is_superuser")
    search_fields = ("username", "full_name", "email")
    ordering = ("username",)
    autocomplete_fields = ()
    filter_horizontal = ("groups", "user_permissions")
    readonly_fields = ("last_login", "date_joined", "updated_at")

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            "Personal details",
            {"fields": ("full_name", "email")},
        ),
        (
            "Role and access",
            {
                "fields": ("role", "is_active", "is_superuser"),
                "description": (
                    "Administrators may manage classrooms, screens and accounts. "
                    "Teachers may only see the classrooms assigned to them."
                ),
            },
        ),
        (
            "Permissions",
            {
                "fields": ("groups", "user_permissions"),
                "classes": ("collapse",),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined", "updated_at")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "full_name",
                    "email",
                    "role",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    def get_queryset(self, request):
        """Avoid N+1 role lookups in the changelist."""
        return super().get_queryset(request).select_related()


# django.contrib.auth registers Group with its own ModelAdmin, which would leave
# it as the one screen an Administrator cannot reach. Re-register it with the
# same role rule so the whole site behaves consistently.
try:
    admin.site.unregister(Group)
except admin.sites.NotRegistered:
    # Only possible if this module is imported before auth's admin module, e.g.
    # from a test that pokes the site directly. Nothing to undo in that case.
    pass


@admin.register(Group)
class StaffGroupAdmin(AdministratorAdminMixin, BaseGroupAdmin):
    """Django's permission groups, restricted to administrators."""