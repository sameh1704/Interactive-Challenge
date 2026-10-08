from __future__ import annotations

from django.apps import AppConfig


class TeacherConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teacher"
    label = "teacher"
    verbose_name = "Teacher workspace"