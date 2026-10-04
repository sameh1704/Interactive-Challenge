"""Application configuration for classrooms."""

from __future__ import annotations

from django.apps import AppConfig


class ClassroomsConfig(AppConfig):
    """Physical classrooms that take part in competitions."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "classrooms"
    verbose_name = "Classrooms"