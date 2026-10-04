"""Helpers for driving real WebSocket connections in tests.

The tests in this package use Channels' ``WebsocketCommunicator``, which speaks
the real ASGI protocol against the real routing table. That matters: a test that
called consumer methods directly would not prove that the URL routes, that
authentication is wired, or that a group broadcast reaches a second socket.

Two things are handled here so the individual tests stay readable:

* **A teacher session.** The teacher endpoint is authenticated from the session
  cookie, so the helper builds a genuine session and passes its key in the scope
  exactly as a browser would.
* **A deadline on every receive.** A test that waits for a message which never
  arrives should fail with a clear message, not hang until the suite times out.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.test import Client

# Generous enough for CI, short enough that a genuinely missing message fails the
# test quickly rather than hanging.
RECEIVE_TIMEOUT_SECONDS = 5

# Establishing a connection performs several database queries, so the accept or
# refusal can take noticeably longer than a subsequent message.
CONNECT_TIMEOUT_SECONDS = 15

# `AllowedHostsOriginValidator` rejects a WebSocket whose Origin is not in
# ALLOWED_HOSTS. A real browser always sends Origin, so the tests must as well -
# otherwise the connection is denied before the consumer ever runs, which is the
# validator working correctly rather than a bug.
TEST_ORIGIN_HOST = "testserver"


def origin_headers() -> list[tuple[bytes, bytes]]:
    return [(b"origin", f"http://{TEST_ORIGIN_HOST}".encode())]


def build_anonymous_scope() -> dict:
    """A base scope for an unauthenticated WebSocket connection.

    No ``session`` key is supplied: ``SessionMiddleware`` instantiates its own
    session wrapper from the request, which is what a real browser connection
    goes through. A screen has no session cookie, so the middleware resolves an
    empty session and the screen authenticates with its Screen ID instead.
    """
    return {
        "type": "websocket",
        "http_version": "1.1",
        "scheme": "ws",
        "query_string": b"",
        "root_path": "",
        "headers": origin_headers(),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


async def make_session_cookie(user) -> str:
    """Create a real authenticated session for ``user`` and return its cookie.

    Signing in goes through Django's own test client rather than writing the
    session by hand, because ``django.contrib.auth.get_user`` needs a complete
    session: ``_auth_user_id`` *and* ``_auth_user_backend`` (and the session
    auth hash). A hand-built session holding only the user id resolves to
    ``AnonymousUser``, so the teacher socket would be refused.

    Writing the session touches the database, so this is a coroutine: it must be
    awaited from an async test body.
    """
    return await database_sync_to_async(_login_cookie)(user)


def _login_cookie(user) -> str:
    client = Client()
    client.force_login(user)
    return client.cookies[settings.SESSION_COOKIE_NAME].value


async def build_teacher_scope(user, competition_id: int) -> dict:
    """Build a scope presenting a real session cookie, as a browser would.

    The session is *not* placed in the scope directly: ``SessionMiddleware``
    builds its own session wrapper from the request, and expects the scope to
    carry the cookie like a genuine connection does. Handing it a SessionStore
    instead would be replaced, and would also raise because the middleware
    treats it as a mapping.
    """
    session_cookie = await make_session_cookie(user)
    path = teacher_url(competition_id)
    scope = build_anonymous_scope()
    scope.update(
        {
            "path": path,
            "raw_path": path.encode(),
            # The cookie is what SessionMiddleware reads to resolve the session.
            "headers": origin_headers()
            + [
                (
                    b"cookie",
                    f"{settings.SESSION_COOKIE_NAME}={session_cookie}".encode(),
                )
            ],
        }
    )
    return scope


def build_anonymous_teacher_scope(competition_id: int) -> dict:
    """A scope with no signed-in user, as an unauthenticated visitor."""
    path = teacher_url(competition_id)
    scope = build_anonymous_scope()
    scope.update({"path": path, "raw_path": path.encode()})
    return scope


def screen_url(screen_id: str) -> str:
    return f"/ws/live/screen/?{urlencode({'screen_id': screen_id})}"


def teacher_url(competition_id: int) -> str:
    return f"/ws/live/teacher/{competition_id}/"


def leaderboard_url(competition_id: int) -> str:
    return f"/ws/live/leaderboard/{competition_id}/"


async def build_leaderboard_scope(user, competition_id: int) -> dict:
    """A signed-in staff scope for the leaderboard display."""
    session_cookie = await make_session_cookie(user)
    path = leaderboard_url(competition_id)
    scope = build_anonymous_scope()
    scope.update(
        {
            "path": path,
            "raw_path": path.encode(),
            "headers": origin_headers()
            + [
                (
                    b"cookie",
                    f"{settings.SESSION_COOKIE_NAME}={session_cookie}".encode(),
                )
            ],
        }
    )
    return scope


async def send_message(communicator, message: dict) -> None:
    """Send one JSON message to the application.

    ``WebsocketCommunicator`` has no ``send_json`` in this Channels version, so
    the text frame is built here. Encoding in one place keeps the tests
    symmetrical with :func:`receive_within`.
    """
    await communicator.send_input(
        {"type": "websocket.receive", "text": json.dumps(message)}
    )


async def receive_within(communicator, timeout: float = RECEIVE_TIMEOUT_SECONDS):
    """Receive one decoded JSON message, or fail with a clear reason.

    ``WebsocketCommunicator`` yields raw ASGI frames rather than decoded
    payloads, so the ``websocket.send`` wrapper is unwrapped and the text is
    parsed here. Anything that is not a text frame - a close, for instance - is
    reported with its type so the failure stays legible.
    """
    try:
        frame = await communicator.receive_output(timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise AssertionError(
            f"No message arrived within {timeout}s. "
            "The expected broadcast did not happen."
        ) from exc

    if frame.get("type") != "websocket.send":
        raise AssertionError(f"Expected a message but received: {frame!r}")

    text = frame.get("text")
    if text is None:
        raise AssertionError(f"Expected a text frame but received: {frame!r}")

    return json.loads(text)


async def drain_until(communicator, event_type: str, limit: int = 12, match=None):
    """Receive messages until one of ``event_type`` arrives.

    Live connections legitimately carry incidental traffic (``welcome``,
    ``state_sync``, ``tick``), so asserting on "the very next message" would make
    these tests brittle. Every message consumed is returned alongside the match,
    so a test can still assert on what preceded it.

    ``match`` is an optional predicate on the payload. Repeated events of the
    same type - a ``presence`` update per connecting screen, say - need it,
    because the first arrival is rarely the one being asserted on.
    """
    seen = []
    for _ in range(limit):
        message = await receive_within(communicator)
        seen.append(message)
        if message.get("type") == event_type and (
            match is None or match(message)
        ):
            return message, seen
    raise AssertionError(
        f"Never received {event_type!r}. Messages seen: "
        f"{[m.get('type') for m in seen]}"
    )


def encode(message: dict) -> str:
    return json.dumps(message)


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "RECEIVE_TIMEOUT_SECONDS",
    "SESSION_KEY",
    "WebsocketCommunicator",
    "build_anonymous_teacher_scope",
    "build_teacher_scope",
    "drain_until",
    "encode",
    "receive_within",
    "screen_url",
    "teacher_url",
]
