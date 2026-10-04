"""Deployment system checks.

These replace the import-time validation that used to live in the settings
module. Settings have to import cleanly for commands such as ``collectstatic``
that run during the image build, so a missing deployment variable is reported
here instead.

``manage.py check`` runs these automatically, and the container entrypoint runs
``manage.py check`` before serving traffic, so a misconfigured deployment fails
immediately and loudly rather than on the first request.
"""

from __future__ import annotations

import os

from django.conf import settings
from django.core.checks import Error, Tags, register

# Variables the application cannot start without.
REQUIRED_VARIABLES = {
    "DJANGO_SECRET_KEY": "Django signing key",
    "DATABASE_NAME": "PostgreSQL database name",
    "DATABASE_USER": "PostgreSQL role",
    "DATABASE_PASSWORD": "PostgreSQL password",
    "REDIS_HOST": "Redis host",
}

# Values that mean "operator forgot to replace the template".
PLACEHOLDER_VALUES = {"changeme", "change-me", "todo", "replace-me", "example"}


@register(Tags.compatibility)
def check_required_environment_variables(app_configs, **kwargs):
    """Every variable the deployment depends on must be set to a real value."""
    errors = []

    for variable, description in REQUIRED_VARIABLES.items():
        value = os.environ.get(variable, "").strip()
        if not value:
            errors.append(
                Error(
                    f"{variable} is not set",
                    hint=f"Set {variable} ({description}) in the environment.",
                    id="challenge.E001",
                )
            )
        elif value.lower() in PLACEHOLDER_VALUES:
            errors.append(
                Error(
                    f"{variable} still holds its placeholder value",
                    hint=f"Replace the placeholder in {variable} with a real value.",
                    id="challenge.E002",
                )
            )

    return errors


@register(Tags.security, deploy=True)
def check_allowed_hosts(app_configs, **kwargs):
    """``ALLOWED_HOSTS`` must be explicit; a wildcard disables host validation."""
    if "*" in settings.ALLOWED_HOSTS:
        return [
            Error(
                "ALLOWED_HOSTS contains a wildcard",
                hint="List the hostnames or IP addresses explicitly.",
                id="challenge.E003",
            )
        ]
    return []