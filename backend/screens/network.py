"""Helpers for reading client network information."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address


def client_ip(request) -> str | None:
    """Return the observed client address, or ``None`` if it cannot be trusted.

    ``X-Forwarded-For`` is attacker-controlled unless a proxy you control is
    known to be rewriting it. It is therefore only honoured when
    ``SCREEN_TRUST_FORWARDED_FOR`` is explicitly enabled, which is the case when
    the application genuinely sits behind a reverse proxy on the school network.
    """

    if settings.SCREEN_TRUST_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            # Left-most entry is the original client; later entries are proxies.
            candidate = _validate(forwarded.split(",")[0].strip())
            if candidate:
                return candidate
            # A malformed header must not erase the address we can observe
            # directly, so fall through to REMOTE_ADDR rather than giving up.

    remote_addr = request.META.get("REMOTE_ADDR", "").strip()
    if remote_addr:
        return _validate(remote_addr)
    return None


def _validate(candidate: str) -> str | None:
    try:
        validate_ipv46_address(candidate)
    except ValidationError:
        return None
    return candidate


def client_user_agent(request) -> str:
    """Return the browser user agent, truncated to the stored length."""
    return request.META.get("HTTP_USER_AGENT", "")[:512]