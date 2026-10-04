"""Channel group naming.

Group names are derived from the database id, never from anything a client
supplied. That matters for security: if a group name were built from a
client-provided string, a screen could nominate a group it does not belong to
and receive another classroom's questions.

The layer's own ``group_name`` restriction (no spaces, no null bytes, ASCII) is
enforced here by construction - the names are built from integer ids.
"""

from __future__ import annotations

SCREEN_GROUP_PREFIX = "competition"
TEACHER_GROUP_PREFIX = "competition-teachers"
LEADERBOARD_GROUP_PREFIX = "competition-leaderboard"


def screen_group(competition_id: int) -> str:
    """Group holding every screen taking part in a competition."""
    return f"{SCREEN_GROUP_PREFIX}.{int(competition_id)}.screens"


def teacher_group(competition_id: int) -> str:
    """Group holding the teacher dashboard(s) for a competition."""
    return f"{TEACHER_GROUP_PREFIX}.{int(competition_id)}"


def screen_lobby_group(competition_id: int) -> str:
    """Group for screens attached to a round but not yet started."""
    return f"{SCREEN_GROUP_PREFIX}.{int(competition_id)}.lobby"


def leaderboard_group(competition_id: int) -> str:
    """Group holding leaderboard displays for a competition.

    Separate from the teacher group because a room often shows the standings on
    its own display, operated by whoever is free rather than by the teacher
    running the round.
    """
    return f"{LEADERBOARD_GROUP_PREFIX}.{int(competition_id)}"
