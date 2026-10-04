"""Admin interface for competitions.

A competition and its ordered questions are set up here by an administrator.
Running a round happens in the live dashboard, not in the admin.
"""

from __future__ import annotations

from django.contrib import admin

from competitions.models import Competition, CompetitionClassroom, CompetitionQuestion
from core.admin import AdministratorAdminMixin
from scoring.admin import ScoringRuleInline


class CompetitionQuestionInline(admin.TabularInline):
    model = CompetitionQuestion
    extra = 1
    fields = ("position", "question", "duration_seconds")
    ordering = ("position",)


class CompetitionClassroomInline(admin.TabularInline):
    model = CompetitionClassroom
    extra = 1
    fields = ("classroom",)


@admin.register(Competition)
class CompetitionAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """Set up a round: its classrooms and its ordered questions."""

    list_display = ("title", "teacher", "state", "question_progress", "started_at")
    list_filter = ("state",)
    search_fields = ("title", "teacher__username", "teacher__full_name")
    ordering = ("-created_at",)
    readonly_fields = (
        "state",
        "current_question",
        "current_question_started_at",
        "current_question_ends_at",
        "question_number",
        "total_questions",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    inlines = (
        CompetitionClassroomInline,
        CompetitionQuestionInline,
        ScoringRuleInline,
    )
    filter_horizontal = ()

    fieldsets = (
        (
            None,
            {
                "fields": ("title", "teacher"),
                "description": (
                    "Include the classrooms below, then add the questions in the "
                    "order they should be asked. Only screens in an included "
                    "classroom may join a live round."
                ),
            },
        ),
        (
            "Live state",
            {
                "classes": ("collapse",),
                "description": (
                    "Managed by the live dashboard. Read-only here so that a "
                    "running competition cannot be edited out from under it."
                ),
                "fields": (
                    "state",
                    "current_question",
                    "current_question_started_at",
                    "current_question_ends_at",
                    "question_number",
                    "total_questions",
                    "started_at",
                    "finished_at",
                ),
            },
        ),
        (
            "Audit",
            {"classes": ("collapse",), "fields": ("created_at", "updated_at")},
        ),
    )

    @admin.display(description="Progress")
    def question_progress(self, obj: Competition) -> str:
        if not obj.total_questions:
            return "—"
        return f"{obj.question_number} / {obj.total_questions}"


@admin.register(CompetitionClassroom)
class CompetitionClassroomAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    list_display = ("competition", "classroom", "created_at")
    list_filter = ("competition",)
    search_fields = ("competition__title", "classroom__name")


@admin.register(CompetitionQuestion)
class CompetitionQuestionAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    list_display = ("competition", "position", "question", "effective_duration_seconds")
    list_filter = ("competition",)
    ordering = ("competition", "position")
