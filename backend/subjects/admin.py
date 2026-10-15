"""Administrator interface for subjects and topics.

Subjects are the one place a name is decided for everyone, so only
administrators manage them: a teacher who could rename "Science" could
re-label every question bank in the school.
"""

from __future__ import annotations

from django.contrib import admin

from core.admin import AdministratorAdminMixin
from subjects.models import Subject, Topic


class TopicInline(admin.TabularInline):
    model = Topic
    extra = 1
    fields = ("name", "slug", "order", "active")


@admin.register(Subject)
class SubjectAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """The curriculum subjects a question bank is organised by."""

    list_display = ("name", "slug", "question_count", "order", "active")
    list_filter = ("active",)
    search_fields = ("name", "slug", "description")
    ordering = ("order", "name")
    prepopulated_fields = {"slug": ("name",)}
    inlines = (TopicInline,)

    fieldsets = (
        (None, {"fields": ("name", "slug", "description")}),
        ("Listing", {"fields": ("order", "active")}),
    )

    @admin.display(description="Questions")
    def question_count(self, obj: Subject) -> int:
        return obj.questions.count()


@admin.register(Topic)
class TopicAdmin(AdministratorAdminMixin, admin.ModelAdmin):
    """Optional subdivisions within a subject."""

    list_display = ("name", "subject", "order", "active")
    list_filter = ("subject", "active")
    search_fields = ("name", "slug", "subject__name")
    ordering = ("subject__order", "order", "name")
