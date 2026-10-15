"""Light / dark theme preference.

The theme follows the same shape as the language preference in
:mod:`django.utils.translation`, and for the same reasons. It is carried in a
cookie rather than in the session or on the user record:

* **A cookie is per browser, so it is per device.** A shared staff-room PC keeps
  the preference whoever is signed in, and one teacher's choice never follows
  another teacher's account onto that machine. Storing it on the user would do
  exactly that.
* **A cookie is not the session.** Django's logout flushes the session, so a
  preference stored there would be forgotten on every sign-out. The theme cookie
  is written outside the session and survives logout, login and a browser
  restart for as long as its max-age says.
* **A cookie is readable by the server.** The active theme is therefore rendered
  straight onto ``<html data-theme="...">``, which means the correct theme is
  the first thing painted: no flash of the wrong one, and the page is already
  correct for a browser with JavaScript switched off.

The cookie is deliberately *not* ``HttpOnly``. Its value is one of two words, it
grants access to nothing, and it carries no personal data; what the toggle buys
with that is an instant switch that does not reload the page. The value is
validated against an allowlist on the way in - both here and in the JavaScript -
so nothing else can be stored under that name, and a tampered cookie falls back
to the default rather than being believed.

Where the preference is *not* stored: ``localStorage``. It would persist across
browsers on the same device, which sounds harmless, but it is invisible to the
server, so the first paint could not know the choice and the page would flash
the wrong theme before JavaScript ran. That is the one thing this module is
built to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.http import HttpResponse

#: The two themes the project ships, in the order the switcher offers them.
LIGHT = "light"
DARK = "dark"
THEMES: tuple[str, ...] = (LIGHT, DARK)

#: Applied when nothing valid is stored: a plain light page. Chosen because it is
#: the safe reading of a school administration interface - high contrast on a
#: projector, no assumption about the room's lighting.
DEFAULT_THEME = LIGHT

#: What the server renders while the browser works out ``prefers-color-scheme``.
#: The head script replaces it before the first paint, so this is only ever seen
#: by a browser with JavaScript disabled.
FALLBACK_THEME = LIGHT


@dataclass(frozen=True)
class Theme:
    """A resolved theme plus the wording the switcher needs to display it."""

    name: str
    is_dark: bool

    @property
    def is_light(self) -> bool:
        return self.name == LIGHT

    @property
    def target(self) -> str:
        """The theme the switcher would switch *to* if pressed now."""
        return DARK if self.is_light else LIGHT


def is_valid(value: object) -> bool:
    """Whether ``value`` is a theme this project actually ships."""
    return isinstance(value, str) and value in THEMES


def resolve(cookie_value: str | None) -> Theme:
    """Turn a stored cookie value into a :class:`Theme`.

    Anything that is not one of the two known themes - a tampered value, a
    truncated cookie, a leftover from a future theme that was since removed -
    resolves to the default rather than being believed. A theme that renders as
    the wrong colours is far worse than one that renders as the default.
    """
    name = cookie_value if is_valid(cookie_value) else DEFAULT_THEME
    return Theme(name=name, is_dark=(name == DARK))


def from_request(request) -> Theme:
    """The theme to render for this request, read from its cookies."""
    return resolve(request.COOKIES.get(settings.THEME_COOKIE_NAME))


def set_cookie(response: HttpResponse, theme: str) -> HttpResponse:
    """Write the theme cookie with the deployment's own attributes."""
    if not is_valid(theme):
        # Refusing rather than writing a default: a caller that passes something
        # unexpected has a bug, and silently persisting DEFAULT_THEME would make
        # an unwanted switch look like a user's choice.
        raise ValueError(f"Unknown theme: {theme!r}")

    response.set_cookie(
        settings.THEME_COOKIE_NAME,
        theme,
        max_age=settings.THEME_COOKIE_AGE,
        path=settings.THEME_COOKIE_PATH,
        domain=settings.THEME_COOKIE_DOMAIN or None,
        secure=settings.THEME_COOKIE_SECURE,
        httponly=settings.THEME_COOKIE_HTTPONLY,
        samesite=settings.THEME_COOKIE_SAMESITE,
    )
    return response


def for_templates(request) -> dict[str, object]:
    """Context for the theme switcher and the ``<html>`` element.

    A context processor rather than a value each view passes along: every page
    that extends the shared base needs it, and a page that forgot to pass it
    would silently drop the switcher instead of failing a test.
    """
    theme = from_request(request)
    return {
        "theme": theme,
        # The JavaScript writes the same cookie the server reads, so it needs the
        # same name and lifetime rather than a second, drifting copy of them.
        "theme_cookie_name": settings.THEME_COOKIE_NAME,
        "theme_cookie_age": settings.THEME_COOKIE_AGE,
    }
