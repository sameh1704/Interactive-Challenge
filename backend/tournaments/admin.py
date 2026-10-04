"""Admin interface for tournaments.

Administrative *correction* lives here, and only here. The tournament pages a
teacher uses cannot edit a concluded championship, and neither can the Hall of
Fame - so if a school's record has to be amended, it is amended by an
administrator in this admin, deliberately and visibly.

Access is restricted by :class:`core.admin.AdministratorAdminMixin` like every
other admin in the project, which is enforced by
``core.tests.test_admin_access``.
"""

from __future__ import annotations

from django.contrib import admin

from core.admin import AdministratorAdminMixin
from tournaments.models import (
    StageAdvancement,
    StageAdvancementEntry,
    StageCompetition,
    Tournament,
    TournamentParticipant,
    TournamentRecord,
    TournamentStage,
)


class TournamentStageInline(admin.TabularInline):
    model = TournamentStage
    extra = 0
    fields = ("order", "stage_type", "name", "status", "advancing_count")
    ordering = ("order",)
    readonly_fields = ("status",)
    show_change_link = True


class TournamentParticipantInline(admin.TabularInline):
    model = TournamentParticipant
    extra = 0
    fields = ("classroom", "status", "current_stage", "aggregate_score")
    readonly_fields = ("status", "aggregate_score")
    show_change_link = True


@admin.register(Tournament)
class TournamentAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """Set up a championship. Running it happens on the tournament pages."""

    list_display = ("name", "season", "subject", "grade", "status", "created_by")
    list_filter = ("status", "subject", "grade", "season")
    search_fields = ("name", "description", "created_by__username", "created_by__full_name")
    ordering = ("-created_at",)
    readonly_fields = ("status", "created_at", "updated_at")
    inlines = (TournamentStageInline, TournamentParticipantInline)

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "description",
                    "subject",
                    "grade",
                    "season",
                    "created_by",
                    "start_date",
                    "end_date",
                )
            },
        ),
        (
            "State",
            {
                "classes": ("collapse",),
                "description": (
                    "Moved by the tournament services, not by hand. A concluded "
                    "championship is a historical record."
                ),
                "fields": ("status",),
            },
        ),
        (
            "Audit",
            {"classes": ("collapse",), "fields": ("created_at", "updated_at")},
        ),
    )


@admin.register(TournamentStage)
class TournamentStageAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    list_display = ("__str__", "stage_type", "order", "status", "advancing_count")
    list_filter = ("stage_type", "status", "tournament")
    search_fields = ("tournament__name", "name")
    ordering = ("tournament__name", "order")


@admin.register(StageCompetition)
class StageCompetitionAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    list_display = ("competition", "stage", "created_at")
    list_filter = ("stage__tournament",)
    search_fields = ("competition__title", "stage__tournament__name")


@admin.register(TournamentParticipant)
class TournamentParticipantAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    list_display = ("classroom", "tournament", "status", "aggregate_score", "current_stage")
    list_filter = ("status", "tournament")
    search_fields = ("classroom__name", "tournament__name")


@admin.register(StageAdvancement)
class StageAdvancementAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """The record of a decided cut.

    Present so an administrator can *see* why a stage is waiting, and can amend a
    championship's history deliberately. Editing one by hand rewrites who advanced,
    so the change is a visible act by a named user.
    """

    list_display = ("stage", "status", "advancing_count", "processed_by", "processed_at")
    list_filter = ("status",)
    search_fields = ("stage__tournament__name",)


@admin.register(StageAdvancementEntry)
class StageAdvancementEntryAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    list_display = ("participant", "advancement", "rank", "score", "correct_answers", "advanced")
    list_filter = ("advanced", "tied")
    search_fields = ("participant__classroom__name",)


@admin.register(TournamentRecord)
class TournamentRecordAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """The frozen conclusion of a championship.

    Read-only by default. A school that needs its historical record corrected
    edits it here, on purpose, rather than having a later change to a past
    competition quietly rewrite a result that was already announced.
    """

    list_display = ("tournament", "season", "winner_name", "completed_at")
    search_fields = ("tournament__name", "winner_name")
    ordering = ("-completed_at",)
    readonly_fields = (
        "tournament",
        "season",
        "subject",
        "grade",
        "winner",
        "winner_name",
        "standings",
        "participants_count",
        "stages_completed",
        "completed_at",
        "generated_at",
    )

    def has_add_permission(self, request) -> bool:
        # A record is produced by finalizing a tournament, never typed in by hand.
        # Amendment happens by editing an existing one.
        return False
