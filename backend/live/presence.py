"""Counting who is attached to a competition.

"Number of connected screens" on the teacher dashboard has to mean *connected
right now*, not "seen recently" - a screen that dropped two seconds ago is not
in the room any more.

Redis group membership is the authoritative answer, because Redis is what
actually tracks which channel names are in a group, and it is shared across web
processes. ``channels_redis`` stores each group as a sorted set, so the count is
a single ``ZCARD``.

Counting has to work without Redis too. The project treats live Redis as
optional in its own test suite (``core.tests.test_redis`` skips when Redis is
unreachable), and consumer tests use the in-memory layer. For those, the
consumers maintain :data:`LOCAL_MEMBERS` themselves, which is exact within one
process - which is all a single-process test run needs.

So: Redis when available, in-process registry otherwise. The registry is a
fallback, never the primary path.
"""

from __future__ import annotations

from collections import defaultdict

from asgiref.sync import async_to_sync

# group name -> set of channel names currently attached in this process.
# Maintained by the screen consumer on connect and disconnect.
LOCAL_MEMBERS: dict[str, set[str]] = defaultdict(set)


def register_member(group_name: str, channel_name: str) -> None:
    LOCAL_MEMBERS[group_name].add(channel_name)


def unregister_member(group_name: str, channel_name: str) -> None:
    LOCAL_MEMBERS[group_name].discard(channel_name)
    if not LOCAL_MEMBERS[group_name]:
        LOCAL_MEMBERS.pop(group_name, None)


def clear_members(group_name: str | None = None) -> None:
    """Reset the registry. Used between tests."""
    if group_name is None:
        LOCAL_MEMBERS.clear()
    else:
        LOCAL_MEMBERS.pop(group_name, None)


async def count_group_members(layer, group_name: str) -> int:
    """Count the channels currently in ``group_name``."""
    redis = _redis_client(layer)

    if redis is None:
        return len(LOCAL_MEMBERS.get(group_name, set()))

    # The key is derived by the layer itself rather than reconstructed here, so
    # this keeps working across channels_redis versions. A group is a sorted set
    # of channel names, so the count is a single ZCARD.
    key = layer._group_key(group_name)
    try:
        return int(await redis.zcard(key))
    except Exception:  # noqa: BLE001
        # A Redis problem must not take the teacher dashboard down. Fall back to
        # this process's own view, which is correct for the common single-node
        # deployment and merely approximate across several.
        return len(LOCAL_MEMBERS.get(group_name, set()))


def count_group_members_sync(layer, group_name: str) -> int:
    """Synchronous wrapper, for use from ``database_sync_to_async`` helpers."""
    try:
        return async_to_sync(count_group_members)(layer, group_name)
    except RuntimeError:
        # Called from inside a running event loop would raise here.
        return len(LOCAL_MEMBERS.get(group_name, set()))


def _redis_client(layer):
    """Return the layer's Redis client, or ``None`` if it has none.

    ``channels_redis`` 4.x exposes the client as ``single_client()``. If that
    attribute ever disappears, returning ``None`` degrades to the in-process
    registry rather than raising, which is the safer failure for a dashboard.
    """
    from channels_redis.core import RedisChannelLayer

    if not isinstance(layer, RedisChannelLayer):
        return None

    getter = getattr(layer, "single_client", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "LOCAL_MEMBERS",
    "clear_members",
    "count_group_members",
    "count_group_members_sync",
    "register_member",
    "unregister_member",
]
