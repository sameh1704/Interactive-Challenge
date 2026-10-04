"""Production settings.

Selected with ``DJANGO_SETTINGS_MODULE=config.settings.production``.

Hardens Django for the future Linux server deployment. TLS termination is
expected to happen in a reverse proxy in front of the container, so the
proxy headers and HSTS values are driven by environment variables.
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403 - re-export the shared settings
from .base import ALLOWED_HOSTS, IS_TESTING, env_bool, env_int

# Never derived from an environment variable: a production deployment must never
# serve debug pages, even if somebody misconfigures the container.
DEBUG = False

if not IS_TESTING and not ALLOWED_HOSTS:
    raise RuntimeError(
        "ALLOWED_HOSTS must list at least one hostname when running in "
        "production. Use hostnames or IP addresses, never a wildcard."
    )

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# Only enable once HTTPS is confirmed working end to end on the target server.
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", default=False)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", default=True)
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", default=False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Uploaded media is served by the front-end proxy, never by Django, so that
# user-supplied files can never be interpreted as application code.
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_MEMORY_SIZE", 5 * 1024 * 1024)
DATA_UPLOAD_MAX_NUMBER_FIELDS = env_int("DATA_UPLOAD_MAX_NUMBER_FIELDS", 1000)
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int("FILE_UPLOAD_MAX_MEMORY_SIZE", 5 * 1024 * 1024)