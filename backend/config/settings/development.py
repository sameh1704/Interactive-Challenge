"""Development settings.

Selected with ``DJANGO_SETTINGS_MODULE=config.settings.development``.

Developer conveniences only. Never use this module on the production server.
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403 - re-export the shared settings
from .base import env_bool

DEBUG = env_bool("DEBUG", default=True)

# Loopback requests may use the Django debug toolbar / SQL logging.
INTERNAL_IPS = ["127.0.0.1", "::1"]