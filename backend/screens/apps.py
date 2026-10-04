"""Application configuration for screens."""

from __future__ import annotations

from django.apps import AppConfig


class ScreensConfig(AppConfig):
    """Interactive classroom screens, identified by a registered Screen ID."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "screens"
    verbose_name = "Interactive screens"