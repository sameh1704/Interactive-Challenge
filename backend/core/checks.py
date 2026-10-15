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


@register(Tags.security, deploy=True)
def check_secure_cookies_match_the_transport(app_configs, **kwargs):
    """A Secure cookie cannot be delivered over plain HTTP.

    ``CSRF_COOKIE_SECURE`` and ``SESSION_COOKIE_SECURE`` are easy to set to True
    on a server that fronts its traffic with a certificate, and equally easy to
    leave at True after that certificate is gone. The symptom is not a warning: a
    browser will not store or send a Secure cookie over an ``http://`` origin, so
    the CSRF cookie never reaches a login POST, Django answers 403
    "CSRF verification failed", and nobody can sign in at all - with correct
    credentials. The page gives no hint which setting caused it, so the cause has
    to be stated here, before anyone spends an afternoon on their password.

    The deployment is judged to be HTTPS when it says so in any of the three
    places it says so: a redirect to https, an HSTS max-age, or a trusted origin
    with an https scheme. With none of those, it is plain HTTP, and Secure
    cookies would break sign-in rather than protect it.
    """
    errors = []

    claims_https = (
        settings.SECURE_SSL_REDIRECT
        or settings.SECURE_HSTS_SECONDS > 0
        or any(str(origin).startswith("https://") for origin in settings.CSRF_TRUSTED_ORIGINS)
    )

    if not claims_https:
        if settings.CSRF_COOKIE_SECURE:
            errors.append(
                Error(
                    "CSRF_COOKIE_SECURE is set on a deployment that does not use https",
                    hint=(
                        "The site is served over plain HTTP, so a browser will drop "
                        "the Secure csrf cookie and every sign-in will fail with 403 "
                        "CSRF verification failed. Set CSRF_COOKIE_SECURE=False."
                    ),
                    id="challenge.E004",
                )
            )
        if settings.SESSION_COOKIE_SECURE:
            errors.append(
                Error(
                    "SESSION_COOKIE_SECURE is set on a deployment that does not use https",
                    hint=(
                        "The site is served over plain HTTP, so a browser will drop "
                        "the Secure session cookie and no sign-in will stick. Set "
                        "SESSION_COOKIE_SECURE=False."
                    ),
                    id="challenge.E005",
                )
            )

    return errors