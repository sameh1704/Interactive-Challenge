"""A minimal WebSocket client, used by the networked checks in this package.

Why hand-rolled
---------------
The container ships no WebSocket client for plain asyncio, and the point of these
scripts is to be an *ordinary* client - the same path a classroom browser takes.
A framework client would bring its own conventions and its own idea of what a
handshake should look like; ~150 lines of RFC 6455 does not, and needs no
dependency that might not be installed in the image.

It also gives these scripts control over exactly what is on the wire, which is
what lets them assert that nothing unexpected was sent.

The Daphne/autobahn quirk
-------------------------
Daphne and autobahn compute ``Sec-WebSocket-Accept`` as ``sha1`` over the base64
key *string* concatenated with the RFC's magic GUID - they do not decode the key
first. A client that follows the letter of the RFC therefore computes a different
answer and is refused. :func:`expected_accept` matches what this server actually
does, and the failure message reports both values so a mismatch is diagnosable
rather than mysterious.

What is deliberately absent
--------------------------
No reconnect logic and no reconnection handling. These scripts drive a scenario
step by step and fail loudly if a message does not arrive, which is the behaviour a
check wants. The classroom screen does have reconnect logic; it lives in
``static/live/js/screen.js`` and is not exercised from here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct

# The RFC 6455 magic GUID, concatenated with the key to form the accept value.
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_TIMEOUT = 10.0


def expected_accept(key: bytes) -> str:
    """The accept value this server expects for a given base64 key."""
    return base64.b64encode(hashlib.sha1(key + WS_GUID).digest()).decode()


class WebSocketClient:
    """One connection, speaking RFC 6455 over a real TCP socket.

    Every frame received is kept in :attr:`messages`, so a check can wait for a
    named event rather than for a position in the stream. The live wire carries
    ticks and presence updates that nobody asked for.
    """

    def __init__(
        self,
        label: str,
        path: str,
        host: str = "127.0.0.1",
        port: int = 8000,
        cookies: dict | None = None,
    ) -> None:
        self.label = label
        self.path = path
        self.host = host
        self.port = port
        self.cookies = cookies or {}
        self.messages: list[dict] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

        key = base64.b64encode(os.urandom(16))
        cookie_header = "; ".join(
            f"{name}={value}" for name, value in self.cookies.items()
        )

        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key.decode()}",
            "Sec-WebSocket-Version: 13",
            f"Origin: http://{self.host}",
        ]
        if cookie_header:
            lines.append(f"Cookie: {cookie_header}")

        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        await self._writer.drain()

        status = await self._reader.readuntil(b"\r\n\r\n")
        header = status.decode("ascii", "replace")

        if " 101 " not in header.split("\r\n")[0]:
            raise AssertionError(
                f"{self.label}: the server refused the socket. "
                f"First line was {header.split(chr(13))[0]!r}."
            )

        if expected_accept(key) not in header:
            raise AssertionError(
                f"{self.label}: bad Sec-WebSocket-Accept. Expected "
                f"{expected_accept(key)!r} in the response headers."
            )

        self._reader_task = asyncio.create_task(self._read_forever())

    async def _read_forever(self) -> None:
        try:
            while True:
                payload, opcode = await self._read_frame()
                if opcode == 0x1:
                    self.messages.append(json.loads(payload.decode("utf-8")))
                elif opcode == 0x8:
                    return
                elif opcode == 0x9:
                    self._write_frame(0xA, payload)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def _read_frame(self) -> tuple[bytes, int]:
        header = await self._reader.readexactly(2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await self._reader.readexactly(8))[0]

        # A server must not mask, but reading the mask keeps the parse honest if
        # it ever does.
        mask = await self._reader.readexactly(4) if masked else b""
        payload = await self._reader.readexactly(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return payload, opcode

    def _write_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        self._writer.write(header + mask + masked)

    async def send(self, message: dict) -> None:
        self._write_frame(0x1, json.dumps(message).encode("utf-8"))
        await self._writer.drain()

    async def wait_for(
        self, event_type: str, timeout: float = DEFAULT_TIMEOUT
    ) -> dict:
        """The first message of ``event_type``, waiting if need be."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for message in self.messages:
                if message.get("type") == event_type:
                    return message
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError(
                    f"{self.label}: no {event_type!r} within {timeout}s. "
                    f"Saw {[m.get('type') for m in self.messages]}."
                )
            await asyncio.sleep(0.02)

    async def wait_for_any(
        self, event_types: tuple, timeout: float = DEFAULT_TIMEOUT
    ) -> dict:
        """The first message whose type is one of ``event_types``."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for message in self.messages:
                if message.get("type") in event_types:
                    return message
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError(
                    f"{self.label}: none of {event_types} within {timeout}s. "
                    f"Saw {[m.get('type') for m in self.messages]}."
                )
            await asyncio.sleep(0.02)

    def seen(self, event_type: str) -> bool:
        return any(m.get("type") == event_type for m in self.messages)

    def count(self, event_type: str) -> int:
        return sum(1 for m in self.messages if m.get("type") == event_type)

    def clear(self) -> None:
        self.messages.clear()

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._writer is not None:
            try:
                self._write_frame(0x8, b"")
                self._writer.close()
            except (ConnectionResetError, RuntimeError):
                pass


__all__ = ["DEFAULT_TIMEOUT", "WebSocketClient", "expected_accept"]