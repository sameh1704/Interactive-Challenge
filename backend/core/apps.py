"""Application configuration for the core app."""

from __future__ import annotations

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Holds the project-level pages that are not owned by a domain app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "Core"

    def ready(self) -> None:
        """Register the deployment system checks defined in ``core.checks``."""
        from core import checks  # noqa: F401 - imported for its side effect