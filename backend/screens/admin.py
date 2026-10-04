"""Admin interface for interactive screens."""

from __future__ import annotations

from django.contrib import admin, messages
from django.utils.html import format_html

from core.admin import AdministratorAdminMixin
from screens.models import InteractiveScreen


@admin.register(InteractiveScreen)
class InteractiveScreenAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """Register and manage interactive screens.

    ``screen_id``, ``last_seen``, ``ip_address`` and ``browser_user_agent`` are
    read-only here on purpose:

    * the Screen ID is generated once and is the screen's permanent identity,
    * the remaining three are recorded by the screen's own heartbeat, and an
      operator typing them in by hand would only create false records.
    """

    list_display = (
        "name",
        "screen_id",
        "classroom_display",
        "primary_badge",
        "status_badge",
        "ip_address",
        "active",
    )
    list_filter = ("active", "is_primary", "classroom__grade")
    search_fields = ("screen_id", "name", "ip_address", "classroom__name")
    ordering = ("name",)
    list_editable = ("active",)
    readonly_fields = (
        "screen_id",
        "last_seen",
        "ip_address",
        "browser_user_agent",
        "registration_url_display",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("name", "screen_id", "registration_url_display")}),
        (
            "Placement",
            {
                "fields": ("classroom", "is_primary", "active"),
                "description": (
                    "A classroom may have one primary screen. Further screens can "
                    "be added later without changing this model."
                ),
            },
        ),
        (
            "Last observation",
            {
                "classes": ("collapse",),
                "fields": ("last_seen", "ip_address", "browser_user_agent"),
                "description": (
                    "Recorded automatically by the screen's heartbeat. These are "
                    "network observations for monitoring only and are never the "
                    "screen's identity."
                ),
            },
        ),
        ("Audit", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Classroom")
    def classroom_display(self, obj: InteractiveScreen) -> str:
        return obj.classroom.display_name if obj.classroom else "—"

    @admin.display(boolean=True, description="Primary")
    def primary_badge(self, obj: InteractiveScreen) -> bool:
        return obj.is_primary

    @admin.display(description="Status")
    def status_badge(self, obj: InteractiveScreen) -> str:
        css = "status-online" if obj.is_online else "status-offline"
        return format_html(f'<span class="badge {css}">{obj.status_display}</span>')

    @admin.display(description="Screen registration URL")
    def registration_url_display(self, obj: InteractiveScreen) -> str:
        """Show the URL the operator should open on the screen.

        Falls back to the relative path when ``PUBLIC_BASE_URL`` is not
        configured, so no address is ever invented.
        """
        url = obj.registration_url
        target = url or obj.registration_path
        if url:
            return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, url)
        return format_html("<code>{}</code>", target)

    def save_model(self, request, obj, form, change):
        """Retiring a screen is worth surfacing, because it stops reporting."""
        was_active = None
        if change:
            was_active = (
                InteractiveScreen.objects.filter(pk=obj.pk)
                .values_list("active", flat=True)
                .first()
            )

        super().save_model(request, obj, form, change)

        if was_active is True and not obj.active:
            messages.info(
                request,
                f"Screen {obj.screen_id} was deactivated. It will no longer "
                "report a status.",
            )