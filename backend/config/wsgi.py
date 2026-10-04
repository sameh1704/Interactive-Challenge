"""WSGI entrypoint.

Provided for completeness and for running the application behind a
conventional WSGI server. The primary runtime uses ASGI (:mod:`config.asgi`)
because Django Channels requires it.
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

application = get_wsgi_application()