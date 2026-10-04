"""Project-level views: the landing page and the health endpoint."""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from core.health import run_checks

logger = logging.getLogger(__name__)


def landing(request: HttpRequest):
    """Render the public landing page."""
    return render(
        request,
        "core/home.html",
        {"app_version": settings.APP_VERSION},
    )


def health(request: HttpRequest) -> JsonResponse:
    """Report application and dependency health as JSON.

    Returns HTTP 200 when every dependency check passes and HTTP 503 otherwise,
    which lets Docker, a reverse proxy or an uptime monitor restart or alert on
    a degraded service without parsing the body.
    """
    checks = run_checks()
    healthy = all(check["status"] == "ok" for check in checks.values())

    payload = {
        "status": "ok" if healthy else "unavailable",
        "service": "challenge-web",
        "version": settings.APP_VERSION,
        "environment": settings.DJANGO_ENV,
        "checks": checks,
        "timestamp": timezone.now().isoformat(),
    }

    return JsonResponse(payload, status=200 if healthy else 503)