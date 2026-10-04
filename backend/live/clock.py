"""Server-authoritative timing.

The single most important rule in this phase: **a browser's own clock is never
trusted.**

If each screen counted down from the moment it happened to receive the
``question_started`` message, screens would drift apart by however long their
connections took to deliver it, and - far worse - a screen whose machine clock
is wrong would show the wrong time entirely. In a live classroom the teacher
looks at one screen and sees the number the students are answering against, so
the screens must agree.

The approach:

1. The server records ``current_question_started_at`` and
   ``current_question_ends_at`` when a question starts.
2. Every ``question_started`` payload carries those absolute timestamps plus the
   server's own current time.
3. Each browser records how far its clock is from the server's, using
   :func:`clock_offset`, and renders its countdown from the server's
   ``ends_at``.

So a screen computes "seconds remaining" from an absolute server deadline
corrected by a measured offset, never from "when did I get this message".
"""

from __future__ import annotations

from datetime import datetime


def remaining_seconds(ends_at: datetime | None, server_now: datetime) -> float:
    """Seconds from ``server_now`` until ``ends_at``, never negative."""
    if ends_at is None:
        return 0.0
    return max(0.0, (ends_at - server_now).total_seconds())


def clock_offset(server_time_iso: str, client_sent_at: float) -> float:
    """Estimate how far the client's clock is behind the server's.

    ``client_sent_at`` is ``Date.now() / 1000`` measured in the browser when the
    message was handled; ``server_time_iso`` is the server's timestamp for the
    same moment. The difference is the offset to subtract from the client's clock.

    Only the server's side of the round trip is used, which makes the estimate
    one-way and avoids the asymmetry of a full round-trip measurement. It is
    good enough for a classroom countdown, and it is corrected again on every
    event rather than trusted indefinitely.
    """
    server_epoch = datetime.fromisoformat(server_time_iso).timestamp()
    return server_epoch - client_sent_at


def seconds_until(deadline: datetime | None) -> float:
    """Seconds remaining against the current server time.

    Used by tests and by the tick loop. Wraps ``timezone.now`` so the database
    is the single source of truth for "now".
    """
    from django.utils import timezone

    return remaining_seconds(deadline, timezone.now())


async def sleep_until(deadline: datetime, *, poll_interval: float = 0.25) -> None:
    """Sleep until ``deadline``, then return.

    Slices the wait rather than sleeping the whole duration in one call so that
    a cancelled request (a teacher skipping ahead, a client disconnecting) is
    noticed promptly instead of at the end of a 30-second sleep.
    """
    import asyncio

    from django.utils import timezone

    while True:
        remaining = (deadline - timezone.now()).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(poll_interval, remaining))


__all__ = [
    "remaining_seconds",
    "clock_offset",
    "seconds_until",
    "sleep_until",
]
