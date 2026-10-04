"""ASGI entrypoint.

Runs Django over ASGI so that Django Channels can handle both ordinary HTTP
requests and WebSocket connections on the same port.

The HTTP and WebSocket routing tables are kept separate in
:mod:`config.routing` so that adding WebSocket consumers later does not touch
this file.
"""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

# The Django application must be initialised before importing anything that
# touches the app registry (for example Channels consumers).
django_asgi_app = get_asgi_application()

from config.routing import websocket_urlpatterns  # noqa: E402
from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
        ),
    }
)