"""Project-level views: the landing page and the health endpoint."""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from core import theme
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


class ThemeSwitchView(View):
    """Store the light/dark preference and return to where it was pressed.

    The switcher's own button is handled by ``theme-manager.js``, which applies
    the theme to the page immediately and *then* posts here, so the teacher sees
    the change without a reload and nothing in a half-filled wizard is touched.
    That makes this view the fallback rather than the mechanism: it is what
    answers a browser with JavaScript disabled, and it is the only writer of the
    cookie, so the cookie's own attributes are set in exactly one place.

    A GET changes nothing and redirects, mirroring Django's ``set_language``: a
    theme is a preference, not a resource to be changed by following a link.
    """

    http_method_names = ["get", "post"]

    def _back(self, request: HttpRequest, fallback: str = "/"):
        """Where to go next: the page the switch was pressed on, if it is ours."""
        next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or fallback
        if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            # A switch to an off-site URL is a bug or an attack; the safe answer
            # is the current site's home page, never the attacker's.
            next_url = fallback
        return redirect(next_url)

    def get(self, request: HttpRequest, *args, **kwargs):
        return self._back(request)

    def post(self, request: HttpRequest, *args, **kwargs):
        chosen = request.POST.get("theme", "")
        if not theme.is_valid(chosen):
            # Not one of THEMES: nothing is written, and the page keeps the theme
            # it was rendered with. An unsupported theme must not be able to put
            # an unknown value into the cookie, because the server would then
            # have to guess what to do with it on the next request.
            logger.info("Rejected theme %r from %s", chosen, request.path)
            return self._back(request)

        response = self._back(request)
        return theme.set_cookie(response, chosen)