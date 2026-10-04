"""Admin interface for questions.

Questions are authored here. A teacher running a live round does not edit
questions, and neither does a screen.
"""

from __future__ import annotations

from django.contrib import admin

from core.admin import AdministratorAdminMixin
from questions.models import Question


@admin.register(Question)
class QuestionAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """Create the questions a competition will draw from."""

    list_display = ("preview", "question_type", "duration_seconds", "scorable", "active")
    list_filter = ("question_type", "active")
    search_fields = ("text",)
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Question")
    def preview(self, obj: Question) -> str:
        return str(obj)

    @admin.display(boolean=True, description="Has a key")
    def scorable(self, obj: Question) -> bool:
        """Whether this question can actually be scored.

        Shown in the list because an unscorable question is otherwise invisible
        until a class is waiting on an answer that can never arrive.
        """
        return obj.has_answer_key

    fieldsets = (
        (None, {"fields": ("text", "question_type")}),
        (
            "Presentation",
            {
                "fields": ("options", "image"),
                "description": (
                    "Multiple choice uses the options list. True/false uses "
                    "neither. The other three types use Type settings below."
                ),
            },
        ),
        (
            "Type settings",
            {
                "fields": ("type_config",),
                "classes": ("wide",),
                "description": (
                    "JSON describing the question, by type. Ordering: "
                    '{"items": [...], "correct_order": [...]}. Classification: '
                    '{"categories": [...], "assignments": [{"label": ..., '
                    '"category": ...}]}. Short answer: {"accepted_answers": [...], '
                    '"case_sensitive": false}. Saved as entered.'
                ),
            },
        ),
        (
            "Answer key",
            {
                "fields": ("correct_option", "correct_answer"),
                "description": (
                    "Multiple choice keys itself with the correct option; "
                    "true/false with the boolean. Every other type keys itself "
                    "through Type settings, and must leave these blank."
                ),
            },
        ),
        (
            "After the reveal",
            {
                "fields": ("explanation",),
                "description": (
                    "Shown to the room only once answering has closed. It is never "
                    "sent to a screen while the question is open."
                ),
            },
        ),
        ("Timing", {"fields": ("duration_seconds",)}),
        (
            "Availability",
            {
                "fields": ("active",),
                "description": (
                    "Inactive questions cannot be added to a competition, but "
                    "existing ones keep their history."
                ),
            },
        ),
        ("Audit", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )
