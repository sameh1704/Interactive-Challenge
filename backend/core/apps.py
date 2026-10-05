"""Application configuration for the core app."""

from __future__ import annotations

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Holds the project-level pages that are not owned by a domain app.

    This module declares exactly one AppConfig on purpose. Django resolves the
    bare ``"core"`` entry in ``INSTALLED_APPS`` by taking the single AppConfig
    subclass here whose ``default`` is truthy, so importing another AppConfig
    subclass into this module - ``django.contrib.admin.apps.AdminConfig`` has
    ``default = True`` - would change what ``"core"`` resolves to. The project's
    admin config therefore lives in ``core.admin_config``; see the note there.
    """

    default_auto_field = "django.bigmodels.BigAutoField"
    name = "core"
    verbose_name = "Core"

    def ready(self) -> None:
        """Register the deployment system checks defined in ``core.checks``."""
        from core import checks  # noqa: F401 - imported for its side effect
