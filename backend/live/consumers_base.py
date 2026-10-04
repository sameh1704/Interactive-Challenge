"""Shared consumer helpers.

Authentication of a WebSocket differs from HTTP in a way that matters here: a
screen has no session, so it cannot be authenticated by a cookie. Instead it
presents its **Screen ID** in the URL and in an ``identify`` message. That makes
the Screen ID a bearer credential, and it is treated as one:

* It is looked up **case-insensitively**, matching the HTTP status page, so an
  operator typing ``am-7kq4xb`` by hand gets the same screen as ``AM-7KQ4XB``.
* It is never logged, never echoed to another client and never used to derive a
  group name.
* An unknown ID and a deactivated ID are refused identically, so the socket
  cannot be used to enumerate registered screens - the same rule the Phase 2
  HTTP endpoints follow.
"""

from __future__ import annotations

import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger(__name__)

# Close code used when a screen's identity is refused. 4400 is in the private
# range reserved for application use, so it cannot be confused with a browser or
# protocol-level failure.
CLOSE_UNAUTHORISED = 4400
CLOSE_INVALID_JSON = 4400


def normalise_screen_id(raw: str | None) -> str:
    """Normalise a presented Screen ID for lookup.

    Surrounding whitespace is removed and the value is upper-cased, matching the
    HTTP status view. The database additionally enforces case-insensitive
    uniqueness, so this cannot match two different screens.
    """
    return (raw or "").strip().upper()


def error_payload(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message}


def reject_payload(code: str, message: str) -> dict:
    return {"type": "rejected", "code": code, "message": message}


class BaseLiveConsumer(AsyncJsonWebsocketConsumer):
    """Common JSON plumbing shared by the screen and teacher consumers.

    Extends :class:`AsyncJsonWebsocketConsumer` - the **async** JSON variant - for
    ``send_json`` and ``receive_json``. The synchronous ``JsonWebsocketConsumer``
    is deliberately not used: it derives from ``SyncConsumer``, which would call
    an ``async def connect`` without awaiting it, so the socket would never be
    accepted or refused and every connection would hang.

    Each concrete consumer declares its own ``receive`` so that malformed frames
    can be answered with a JSON error rather than closing the socket.
    """

    async def ws_receive(self, text_data: str = None, bytes_data: bytes = None) -> None:
        raise NotImplementedError


def load_screen(screen_id: str):
    """Look up an active screen by its normalised Screen ID.

    Returns ``None`` for unknown, inactive and blank IDs alike. Deliberately a
    single query that joins the classroom, because the classroom is needed to
    authorise the join on the very next line.
    """
    from screens.models import InteractiveScreen

    if not screen_id:
        return None

    return (
        InteractiveScreen.objects.active()
        .filter(screen_id=screen_id)
        .select_related("classroom")
        .first()
    )


__all__ = [
    "AsyncWebsocketConsumer",
    "BaseLiveConsumer",
    "CLOSE_UNAUTHORISED",
    "error_payload",
    "load_screen",
    "normalise_screen_id",
    "reject_payload",
    "sync_to_async",
    "logger",
]
