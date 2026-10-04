"""Screen-facing views.

These are the only views a screen device can reach, and they are deliberately
unauthenticated: an interactive screen has no operator sitting at it. The
security model is therefore:

* A screen may only see **its own** record: name, Screen ID, status and its
  classroom's name.
* A screen may only ever **write** ``last_seen``, ``ip_address`` and
  ``browser_user_agent``. Identity, classroom and role are never writable, so a
  heartbeat cannot move a screen into another classroom.
* No teacher account, no other screen, no credentials and no administration
  function is reachable from here. Creating, editing and deactivating screens
  happen in the Django admin, which only administrators can enter.
* An unknown Screen ID and an inactive Screen ID produce the **same** response,
  so the endpoint cannot be used to enumerate registered screens.
* Screen ID presentation is rate limited per client address, because the Screen
  ID is the only credential a screen has (see :mod:`core.ratelimit`).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core import ratelimit as core_ratelimit
from screens.models import InteractiveScreen
from screens.network import client_ip, client_user_agent

logger = logging.getLogger(__name__)

#: Response returned when a client has presented too many Screen IDs.
#:
#: The **body** is the same unregistered-Screen-ID body an unknown ID produces,
#: so the response still says nothing about whether the ID exists - that is the
#: anti-enumeration guarantee. The **status** deliberately differs from the 404 an
#: unknown ID gets: a throttled client is told to wait and try again, which is the
#: difference between a useful answer and a dead end, and "this address is being
#: throttled" is not information about any Screen ID.
TOO_MANY_ATTEMPTS = 429


def throttled(request: HttpRequest, screen_id: str = ""):
    """The over-limit response for the HTML endpoints.

    ``Retry-After`` carries the window from :mod:`core.ratelimit`, so a screen that
    reconnects on a backoff is told how long to wait rather than retrying straight
    back into the same refusal.
    """
    response = render(
        request,
        "screens/unregistered.html",
        {"screen_id": screen_id},
        status=TOO_MANY_ATTEMPTS,
    )
    response["Retry-After"] = str(max(1, int(settings.SCREEN_RATE_LIMIT_WINDOW)))
    return response


@require_GET
def screen_status(request: HttpRequest):
    """The page a screen device opens, e.g. ``/screen/?screen_id=AM-7KQ4XB``.

    Renders either the screen's own status page or a generic "not registered"
    page. Being unregistered and being throttled produce the same page; only the
    status and ``Retry-After`` differ.
    """
    if not core_ratelimit.screen_identifier_allowed(client_ip(request)):
        return throttled(request)

    screen_id = (request.GET.get("screen_id") or "").strip().upper()

    screen = _lookup_screen(screen_id)

    if screen is None:
        return render(
            request,
            "screens/unregistered.html",
            {"screen_id": screen_id},
            status=404,
        )

    return render(
        request,
        "screens/status.html",
        {
            "screen": screen,
            "classroom": screen.classroom,
            "status_display": screen.status_display,
            "heartbeat_interval_seconds": settings.SCREEN_HEARTBEAT_INTERVAL_SECONDS,
        },
    )


@require_POST
def screen_heartbeat(request: HttpRequest):
    """Record that a screen is present.

    Returns a minimal payload: enough for the screen to render itself, and
    nothing about the rest of the system.
    """
    if not core_ratelimit.screen_identifier_allowed(client_ip(request)):
        response = JsonResponse(
            {"registered": False, "detail": "This screen is not registered."},
            status=TOO_MANY_ATTEMPTS,
        )
        response["Retry-After"] = str(
            max(1, int(settings.SCREEN_RATE_LIMIT_WINDOW))
        )
        return response

    screen_id = (request.POST.get("screen_id") or "").strip().upper()

    screen = _lookup_screen(screen_id)
    if screen is None:
        return JsonResponse(
            {"registered": False, "detail": "This screen is not registered."},
            status=404,
        )

    screen.record_heartbeat(
        ip_address=client_ip(request),
        user_agent=client_user_agent(request),
    )

    return JsonResponse(
        {
            "registered": True,
            "screen_id": screen.screen_id,
            "status": screen.status,
            "last_seen": screen.last_seen.isoformat(),
            "classroom": screen.classroom.display_name if screen.classroom else None,
        }
    )


def _lookup_screen(screen_id: str) -> InteractiveScreen | None:
    """Find an active screen by its Screen ID, or return ``None``.

    Inactive screens are treated exactly like unknown ones, so that the endpoint
    cannot be used to discover which Screen IDs exist.
    """
    if not screen_id:
        return None

    screen = (
        InteractiveScreen.objects.active()
        .select_related("classroom")
        .filter(screen_id=screen_id)
        .first()
    )

    if screen is None:
        logger.info("Heartbeat or status request for an unregistered Screen ID.")

    return screen