"""Admin for answers, scoring rules and results.

Answers are **read-only** here. An answer is a historical fact: a round's
standings are derived from it and a result has already been announced to a room
of students. Allowing an answer to be edited would mean the leaderboard and the
record could disagree, and nothing on the page would say which one was true.

Correcting a mistaken answer is therefore an explicit, visible act - the
admin deactivates the question and the teacher re-runs the round - rather than a
quiet edit that leaves no trace.
"""

from __future__ import annotations

from django.contrib import admin

from core.admin import AdministratorAdminMixin
from scoring.models import Answer, CompetitionResult, ScoringRule


class ScoringRuleInline(admin.StackedInline):
    model = ScoringRule
    can_delete = False
    extra = 0
    max_num = 1
    fields = ("correct_points", "speed_bonus_max", "speed_bonus_mode")


@admin.register(Answer)
class AnswerAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """The answer history behind every result."""

    list_display = (
        "competition",
        "position",
        "classroom",
        "selection",
        "is_correct",
        "response_time_seconds",
        "points",
        "speed_bonus",
        "total",
    )
    list_filter = ("competition", "is_correct")
    search_fields = ("classroom__name", "selected_answer", "competition__title")
    ordering = ("-competition_id", "position", "classroom__name")
    readonly_fields = [field.name for field in Answer._meta.fields]

    @admin.display(description="Total")
    def total(self, obj: Answer) -> int:
        return obj.total_score

    def has_add_permission(self, request, obj=None) -> bool:
        """Answers are created by screens during a live round, not typed in."""
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ScoringRule)
class ScoringRuleAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """Per-competition marks. A competition with no row uses the defaults."""

    list_display = ("competition", "correct_points", "speed_bonus_max", "speed_bonus_mode")
    list_filter = ("speed_bonus_mode",)
    search_fields = ("competition__title",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(CompetitionResult)
class CompetitionResultAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """A frozen final standings snapshot."""

    list_display = (
        "competition",
        "winner_name",
        "total_questions",
        "answered_count",
        "generated_at",
    )
    search_fields = ("competition__title", "winner_name")
    ordering = ("-generated_at",)
    readonly_fields = [field.name for field in CompetitionResult._meta.fields]

    def has_add_permission(self, request, obj=None) -> bool:
        """A result is generated when a round finishes, not typed in."""
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False