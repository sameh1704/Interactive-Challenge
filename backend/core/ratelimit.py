"""Cache-backed rate limiting for the two credentials the application has.

The application presents exactly two secrets to a client, and both are guesses
that can be automated:

* a **Screen ID** - an interactive screen has no session, so ``AM-7KQ4XB`` is a
  bearer credential that authorises that screen's classroom into a competition.
* a **password** - the only thing standing between the network and the teacher
  controls, the question bank and the championships.

Neither is length-limited by a lockout anywhere else, so without this module an
attacker with any route to the server could present unlimited guesses. The limit
is per observed client address, and deliberately generous: a whole classroom can
sit behind one NAT address, and an interactive screen reconnects on its own, so a
tight limit would lock out a legitimate lesson rather than a guesser.

Design notes
------------
* **Fails open.** If the cache is unreachable the counter is skipped and the
  attempt is allowed, with a warning logged. A limiter that takes the school
  offline when Redis blinks is a worse failure than the one it prevents; the
  health endpoint already reports Redis as unavailable, so the condition is
  visible rather than silent.
* **Counts identifiers by digest.** The key is a hash, so a caller cannot put
  arbitrary text - a chosen password, say - into the cache key space.
* **Window is fixed per counter, not sliding.** A fixed window can let a caller
  through at 2x the limit across a boundary. That is acceptable here because the
  limit is a brake on an online guessing attack, not a quota.
"""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


def _digest(namespace: str, identifier: str) -> str:
    """Build a cache key that cannot grow without bound or leak its input."""
    hashed = hashlib.sha256(identifier.encode("utf-8", "replace")).hexdigest()
    return f"ratelimit:{namespace}:{hashed}"


def allow(namespace: str, identifier: str, *, limit: int, window: int) -> bool:
    """Record one attempt and report whether it is permitted.

    Returns ``True`` while the caller is within ``limit`` attempts per ``window``
    seconds, and ``False`` once the limit is exceeded.
    """
    if limit <= 0:
        # A limit of zero would mean "never allow"; treat it as "no limit" so a
        # misconfigured environment variable cannot silently break sign-in.
        return True

    key = _digest(namespace, identifier or "-")

    try:
        count = cache.get(key)
        if count is None:
            cache.set(key, 1, timeout=window)
            return True
        count = cache.incr(key)
    except ValueError:
        # incr() raises when the key expired between the get and the incr.
        try:
            cache.set(key, 1, timeout=window)
        except Exception:  # pragma: no cover - cache backend is unavailable
            logger.warning("Rate limit counter for %s unavailable; allowing.", namespace)
            return True
        return True
    except Exception:
        logger.warning("Rate limit counter for %s unavailable; allowing.", namespace)
        return True

    if count > limit:
        logger.info(
            "Rate limit reached for %s (attempt %s of %s in %ss).",
            namespace,
            count,
            limit,
            window,
        )
        return False

    return True


def screen_identifier_allowed(client_address: str | None) -> bool:
    """Whether another Screen ID may be presented from this address.

    Applied to the screen status page, the heartbeat and the screen WebSocket,
    so guessing is throttled whether it is done over HTTP or a socket.
    """
    return allow(
        "screen-id",
        client_address or "unknown",
        limit=settings.SCREEN_RATE_LIMIT,
        window=settings.SCREEN_RATE_LIMIT_WINDOW,
    )


def login_allowed(username: str, client_address: str | None) -> bool:
    """Whether another sign-in attempt may be made.

    Counted twice, and both counters have to pass:

    * **per username, globally.** Keyed on the username alone, so an attacker who
      spreads one password-guessing run across many source addresses still gets
      ``LOGIN_RATE_LIMIT`` attempts at that account per window. This is the
      counter that actually bounds an online guessing attack.
    * **per address.** Keyed on the address alone, so one machine cycling through
      thousands of usernames is bounded too - and so a distributed username spray
      cannot spend the whole budget.

    A successful sign-in consumes the quota exactly as a wrong password does.
    The counter is on *attempts*, because whether an attempt would have succeeded
    is what an attacker controls, and what must not change what an attempt costs.
    The price is that a shared machine cannot sign in more than
    ``LOGIN_RATE_LIMIT`` times inside the window even with the right password; the
    window passes and it works again.
    """
    name = (username or "").strip().lower()
    by_name = allow(
        "login-name",
        name or "-",
        limit=settings.LOGIN_RATE_LIMIT,
        window=settings.LOGIN_RATE_LIMIT_WINDOW,
    )
    by_address = allow(
        "login-address",
        client_address or "unknown",
        limit=settings.LOGIN_RATE_LIMIT * settings.LOGIN_RATE_LIMIT_ACCOUNT_FACTOR,
        window=settings.LOGIN_RATE_LIMIT_WINDOW,
    )
    return by_name and by_address


__all__ = [
    "allow",
    "login_allowed",
    "screen_identifier_allowed",
]