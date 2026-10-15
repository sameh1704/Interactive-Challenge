"""Base settings shared by every environment.

Every deployment-specific value is read from the process environment. No secret,
credential, host address or encryption key is ever stored in this repository.

Select an environment by pointing ``DJANGO_SETTINGS_MODULE`` at
``config.settings.development`` or ``config.settings.production``.
"""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

from config.logging_config import build_logging_config

# backend/ -> /app inside the container image.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------


def env_str(name: str, default: str = "") -> str:
    """Return a raw environment string. Values are never stripped or coerced."""
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"Environment variable {name} must be an integer, got {raw!r}."
        ) from exc


def env_list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = os.environ.get(name, "")
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(default)


# ---------------------------------------------------------------------------
# Environment identity
# ---------------------------------------------------------------------------

DJANGO_ENV = env_str("DJANGO_ENV", "development").strip().lower()
APP_VERSION = env_str("APP_VERSION", "0.1.0")

# ``manage.py test`` disables DEBUG, which makes ALLOWED_HOSTS mandatory. The
# Django test client identifies itself as "testserver", so allow it while tests
# run instead of forcing operators to add it to the production host list.
IS_TESTING = "test" in sys.argv


# ---------------------------------------------------------------------------
# Core security settings
# ---------------------------------------------------------------------------


def _resolve_secret_key() -> str:
    key = env_str("DJANGO_SECRET_KEY").strip()
    if key:
        return key

    message = (
        "DJANGO_SECRET_KEY is not set. Generate one with: "
        'python -c "from django.core.management.utils import '
        'get_random_secret_key; print(get_random_secret_key())"'
    )
    if DJANGO_ENV == "production":
        raise ImproperlyConfigured(message)

    # Development only: keep the stack startable on a fresh clone without
    # committing a key to the repository. The key changes on every restart, so
    # sessions do not survive a container bounce.
    warnings.warn(f"{message} Falling back to an ephemeral key.", RuntimeWarning)
    return get_random_secret_key()


SECRET_KEY = _resolve_secret_key()

DEBUG = env_bool("DEBUG", default=False)

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", default=("localhost", "127.0.0.1", "[::1]"))
if IS_TESTING and "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("testserver")

CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")


# ---------------------------------------------------------------------------
# Authentication and authorisation
# ---------------------------------------------------------------------------
#
# The user model is defined in the `accounts` application so that a role can live
# on the user row. This must be set before any migration is created or run.

AUTH_USER_MODEL = "accounts.User"

# Listed explicitly rather than relying on the global default, so that adding an
# AD/LDAP backend later is a one-line change instead of a settings reshuffle.
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "core:landing"


# One password rule, deliberately a floor rather than a composition rule.
#
# A school needs passwords its staff can actually choose: typed on a keypad,
# written on a whiteboard, reset by a non-specialist administrator. Rules like
# "one uppercase, one digit, one symbol" are reliably worked around with
# `Password1` and then shared across a desk, which is worse than a slightly
# longer single word. Eight characters is long enough to make an unthrottled
# guessing run hopeless and short enough not to obstruct anybody.
#
# Online guessing is separately bounded by LOGIN_RATE_LIMIT (see the rate
# limiting section below), so this rule is about the *chosen* password, not
# about the rate: it stops an account being protected by a four-character guess.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        # Django's own default is 8. Written out so the number is a decision
        # recorded here rather than a framework default that could change.
        "OPTIONS": {"min_length": 8},
    }
]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
#
# How long a signed-in session stays valid, in seconds.
#
# The decision: twelve hours, not the framework default of fourteen days.
#
# A teacher's day is one contiguous block - sign in before first period, stay
# signed in through the day, sign out at the end. Twelve hours covers that with
# room to spare, including a late-afternoon competition, so nobody is asked to
# sign in mid-lesson. What it deliberately does not cover is a device left
# signed in overnight on a shared staff-room machine: with a fortnight-long
# cookie, a session stolen from such a machine stays usable for two weeks, which
# is the whole window an attacker needs to sit in the competition engine and
# the question bank.
#
# Twelve hours is also comfortably longer than any lesson, so a session cannot
# expire mid-question. A teacher who is signed out simply signs in again.
SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE", 12 * 60 * 60)


# ---------------------------------------------------------------------------
# Interactive screens
# ---------------------------------------------------------------------------

# A screen is reported "online" while its last heartbeat is within this window.
# It must comfortably exceed the heartbeat interval, or screens will flicker
# between online and offline between two reports.
SCREEN_ONLINE_WINDOW_SECONDS = env_int("SCREEN_ONLINE_WINDOW_SECONDS", 90)

# How often the screen page asks the server to record a heartbeat.
SCREEN_HEARTBEAT_INTERVAL_SECONDS = env_int("SCREEN_HEARTBEAT_INTERVAL_SECONDS", 30)

# X-Forwarded-For is attacker-controlled unless a proxy you control rewrites it.
# Leave this off unless the application really is behind a reverse proxy.
SCREEN_TRUST_FORWARDED_FOR = env_bool("SCREEN_TRUST_FORWARDED_FOR", default=False)

# Absolute base address of the application, used only to render the URL an
# operator should open on a screen. Deliberately empty by default: the
# application must not assume any address, and works on any server.
PUBLIC_BASE_URL = env_str("PUBLIC_BASE_URL").rstrip("/")


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    # Must precede staticfiles so that `runserver` uses the ASGI stack.
    "daphne",
    # Django's own admin app config, carrying `default_site` so `/admin/` is
    # served by core.admin_site.ChallengeAdminSite. It keeps the app's `name`,
    # so it replaces "django.contrib.admin" rather than joining it - listing
    # both would make Django's `default = True` config the winner and silently
    # ignore the custom site.
    "core.admin_config.ChallengeAdminConfig",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "core",
    "accounts",
    "classrooms",
    "screens",
    "subjects",
    "questions",
    "competitions",
    "live",
    "scoring",
    "tournaments",
    "reports",
    "teacher",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.StageDeputyBoundaryMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Searched *before* any installed app. This is what lets the project's
        # `admin/base_site.html`, `admin/nav_sidebar.html` and `admin/index.html`
        # override Django's, which an app-level override cannot: `core` is listed
        # after `django.contrib.admin`, and the app template loader walks
        # INSTALLED_APPS in order, so Django's own copies would win.
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                # The active light/dark theme, for the `data-theme` attribute on
                # <html> and the switcher form in the shared masthead.
                "core.context_processors.theme_context",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database (PostgreSQL is the only supported backend)
# ---------------------------------------------------------------------------
#
# This module must always import successfully. Commands such as `collectstatic`
# and `makemigrations` run during the image build, long before any database or
# cache exists, so a missing runtime variable cannot be turned into an
# import-time exception here. Missing variables are reported by the deployment
# system checks in `core.checks`, which the container entrypoint runs before
# serving traffic.

DATABASE_NAME = env_str("DATABASE_NAME")
DATABASE_USER = env_str("DATABASE_USER")
DATABASE_PASSWORD = env_str("DATABASE_PASSWORD")
DATABASE_HOST = env_str("DATABASE_HOST", "localhost")
DATABASE_PORT = env_int("DATABASE_PORT", 5432)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": DATABASE_NAME,
        "USER": DATABASE_USER,
        "PASSWORD": DATABASE_PASSWORD,
        "HOST": DATABASE_HOST,
        "PORT": str(DATABASE_PORT),
        "CONN_MAX_AGE": env_int("DATABASE_CONN_MAX_AGE", 60 if not IS_TESTING else 0),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": env_int("DATABASE_CONNECT_TIMEOUT", 10),
        },
        "TEST": {
            "NAME": env_str("DATABASE_TEST_NAME") or None,
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Redis: cache backend plus the Channels layer transport
# ---------------------------------------------------------------------------

REDIS_HOST = env_str("REDIS_HOST", "localhost")
REDIS_PORT = env_int("REDIS_PORT", 6379)
REDIS_DB = env_int("REDIS_DB", 0)
REDIS_PASSWORD = env_str("REDIS_PASSWORD")

if REDIS_PASSWORD:
    REDIS_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
else:
    REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": env_str("CACHE_KEY_PREFIX", "almanar-challenge"),
        "TIMEOUT": env_int("CACHE_TIMEOUT", 300),
    }
}

# How long a channel-layer group membership survives without traffic. It must be
# far longer than the gap a real lesson can leave between two teacher actions,
# because an expired membership silently stops a screen receiving broadcasts -
# the board keeps showing the old question with nothing to indicate it is stale.
# The keepalive in live.heartbeat is the primary defence; this is the backstop
# for when the keepalive itself cannot run.
CHANNEL_LAYER_EXPIRY_SECONDS = env_int("CHANNEL_LAYER_EXPIRY", 300)

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            # `socket_timeout` must be off, and this is not a tuning preference.
            #
            # The layer receives by blocking on BZPOPMIN for `brpop_timeout`
            # seconds at a time. redis-py 8 defaults `socket_timeout` to 5s,
            # which is the same length as that block, so the client-side read
            # timeout wins the race: Redis has nothing to say, the client gives
            # up, and `redis.exceptions.TimeoutError` propagates out of the
            # ASGI application and drops the WebSocket. A screen sitting idle
            # between two questions would be disconnected roughly every five
            # seconds, with nothing on the board to show it.
            #
            # With no client-side timeout the block is bounded by the layer's
            # own `brpop_timeout`, which is what it is designed around.
            "hosts": [{"address": REDIS_URL, "socket_timeout": None}],
            "capacity": env_int("CHANNEL_LAYER_CAPACITY", 1500),
            "expiry": CHANNEL_LAYER_EXPIRY_SECONDS,
        },
    }
}


# ---------------------------------------------------------------------------
# Live competitions
# ---------------------------------------------------------------------------

# How often an attached screen is sent a clock beacon, in seconds. The beacon
# carries the server time so a screen can correct its own countdown rather than
# trusting the machine clock on the wall. Must comfortably exceed the WebSocket
# ping interval so a healthy connection stays open.
LIVE_TICK_INTERVAL_SECONDS = env_int("LIVE_TICK_INTERVAL_SECONDS", 5)

# How often the server broadcasts to every round it is hosting, in seconds. This
# is the keepalive that stops an idle screen's group membership lapsing - see
# live.heartbeat. Must be well under CHANNEL_LAYER_EXPIRY, which is the backstop
# if the keepalive cannot run.
LIVE_HEARTBEAT_SECONDS = env_int("LIVE_HEARTBEAT_SECONDS", 15)

# A question shorter than this is refused, and a competition may not have more
# questions than this. Both are configuration so a school can adjust them without
# a code change.
MIN_QUESTION_DURATION_SECONDS = env_int("MIN_QUESTION_DURATION_SECONDS", 5)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# The defaults used by any competition that has no scoring rule of its own, so a
# round works out of the box and a school can set its own marks without a code
# change. A competition may override all three in the admin.
SCORING_CORRECT_POINTS = env_int("SCORING_CORRECT_POINTS", 100)

# Most extra points available for answering quickly, and how they are earned.
# `linear` pays the full bonus at the start of a question, decaying to nothing by
# its deadline; `none` disables the bonus whatever the maximum says.
SCORING_SPEED_BONUS_MAX = env_int("SCORING_SPEED_BONUS_MAX", 50)
SCORING_SPEED_BONUS_MODE = env_str("SCORING_SPEED_BONUS_MODE", "linear")

# Seconds a screen waits before retrying after a dropped connection. Bounded
# exponential backoff is applied on top of this so that a server restart does not
# bring every screen back in the same instant.
LIVE_RECONNECT_DELAY_SECONDS = env_int("LIVE_RECONNECT_DELAY_SECONDS", 2)

# Ceiling for the reconnect backoff, so a screen that has been away for a while
# retries quickly again.
LIVE_RECONNECT_MAX_DELAY_SECONDS = env_int("LIVE_RECONNECT_MAX_DELAY_SECONDS", 15)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
#
# The application presents exactly two secrets to a client: a Screen ID, which
# authorises a screen's classroom into a competition, and a password, which
# guards every teacher and administrator function. Both are guesses a script can
# automate, so both are counted per observed client address by
# `core.ratelimit`. See that module for why the limits are deliberately generous
# and why the limiter fails open.

# A classroom can sit behind one NAT address and every screen reconnects on its
# own, so this has to be well above "one page load plus a heartbeat" per screen.
SCREEN_RATE_LIMIT = env_int("SCREEN_RATE_LIMIT", 120)
SCREEN_RATE_LIMIT_WINDOW = env_int("SCREEN_RATE_LIMIT_WINDOW", 60)

# Counted per username and per address. The address limit is the per-username
# limit multiplied by this, so many usernames from one address are still bounded.
LOGIN_RATE_LIMIT = env_int("LOGIN_RATE_LIMIT", 10)
LOGIN_RATE_LIMIT_WINDOW = env_int("LOGIN_RATE_LIMIT_WINDOW", 300)
LOGIN_RATE_LIMIT_ACCOUNT_FACTOR = env_int("LOGIN_RATE_LIMIT_ACCOUNT_FACTOR", 5)


# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("ar", "العربية"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]

# How long the language cookie survives, in seconds.
#
# Django's own default is None, which makes the cookie a *session* cookie: it
# disappears the moment the browser closes. That is wrong for this deployment.
# A teacher who picks Arabic should not be handed English again the next morning
# because the browser was restarted overnight, and a teacher signing out at the
# end of a lesson and back in the next one must not have to pick again.
#
# It is deliberately keyed to the same lifetime as the session cookie rather than
# a longer one: the language is a preference held alongside a signed-in identity,
# so it should expire with the session rather than linger on a shared staff-room
# machine for weeks. It is stored in its own cookie, not in the Django session,
# so signing out (which flushes the session) does not clear it.
LANGUAGE_COOKIE_AGE = env_int("LANGUAGE_COOKIE_AGE", SESSION_COOKIE_AGE)

# The cookie Django's `set_language` view writes and `LocaleMiddleware` reads on
# every following request. Django 5.2's `get_language_from_request` reads the
# cookie, not the session, so this cookie is the carrier of the active language
# between requests. Keeping Django's own name also keeps the view compatible with
# the documentation, and any bookmark that carries the cookie keeps working.
LANGUAGE_COOKIE_NAME = env_str("LANGUAGE_COOKIE_NAME", "django_language")
LANGUAGE_COOKIE_PATH = env_str("LANGUAGE_COOKIE_PATH", "/")
LANGUAGE_COOKIE_DOMAIN = env_str("LANGUAGE_COOKIE_DOMAIN", "")

# The language cookie carries no identity and nothing sensitive: it is a
# two-character display preference, and the page already prints the chosen
# language. There is nothing here for JavaScript to steal, and marking it
# unreadable would only make a future client-side switch unable to show the
# current choice.
LANGUAGE_COOKIE_HTTPONLY = False

# SameSite=Lax: the language switch is a POST (the masthead switcher posts to
# `/i18n/setlang/`), and Lax both protects that POST and still sends the cookie
# on every ordinary top-level navigation afterwards.
LANGUAGE_COOKIE_SAMESITE = "Lax"

# Secure flag: Django's default is False, which is correct for a school server
# still serving plain HTTP. Production mirrors SESSION_COOKIE_SECURE, so the
# language cookie is flagged Secure on exactly the same transports that can
# carry the session cookie.

TIME_ZONE = env_str("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# --- theme cookie (light / dark) ------------------------------------------------
#
# The theme is a per-device display preference, so it is a cookie and not part of
# the session (which logout flushes) and not a field on the user (which would
# make one teacher's choice follow another onto a shared machine). See
# `core/theme.py` for the reasoning in full.
#
# It is not HttpOnly: `theme-manager.js` writes it so the switch can apply
# without a page reload, and it is written server-side too, so the deployment's
# attributes are the ones the browser keeps. The value is one of two words and
# grants access to nothing.
THEME_COOKIE_NAME = env_str("THEME_COOKIE_NAME", "almanar_theme")

# A year: the requirement is that the choice survives a browser restart, which a
# session cookie cannot do. It is not keyed to the twelve-hour session because a
# display preference is not a credential - it is the same argument as the desk
# lamp being left on. `almanar_theme` is not sensitive, so a long life carries no
# risk that a short one would avoid.
THEME_COOKIE_AGE = env_int("THEME_COOKIE_AGE", 365 * 24 * 60 * 60)

THEME_COOKIE_PATH = env_str("THEME_COOKIE_PATH", "/")
THEME_COOKIE_DOMAIN = env_str("THEME_COOKIE_DOMAIN", "")

# The toggle posts a form, so the POST needs SameSite protection; the cookie it
# leaves behind must then be sent on every ordinary top-level navigation. Lax is
# the value that does both.
THEME_COOKIE_SAMESITE = "Lax"

# Not HttpOnly, so the JavaScript can write it (see above). The value it carries
# is not a secret and not personal data: it is one of two words.
THEME_COOKIE_HTTPONLY = False

# Secure flag defaults to off, matching Django and matching a school server that
# is still serving plain HTTP. Production mirrors SESSION_COOKIE_SECURE, so the
# cookie is flagged Secure on exactly the transports that can already carry the
# session - the same rule the language cookie follows.
THEME_COOKIE_SECURE = False


# ---------------------------------------------------------------------------
# Static files and media files are kept strictly separate
# ---------------------------------------------------------------------------

STATIC_URL = env_str("STATIC_URL", "/static/")
STATIC_ROOT = BASE_DIR / env_str("STATIC_ROOT_DIR", "staticfiles")

MEDIA_URL = env_str("MEDIA_URL", "/media/")
MEDIA_ROOT = BASE_DIR / env_str("MEDIA_ROOT_DIR", "media")

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # Selected from DJANGO_ENV rather than DEBUG: this module is imported
        # before `development`/`production` override DEBUG, so keying off DEBUG
        # here would let the two modules disagree about the storage backend.
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if DJANGO_ENV == "production"
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}


# ---------------------------------------------------------------------------
# Logging: structured console output for container log collectors
# ---------------------------------------------------------------------------

LOG_LEVEL = env_str("LOG_LEVEL", "INFO").strip().upper()
LOGGING = build_logging_config(LOG_LEVEL, DJANGO_ENV)
