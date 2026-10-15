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

#: Every screen that has identified itself and has no competition to show yet.
#: See ScreenConsumer.waiting_group for why this exists rather than retrying.
WAITING_GROUP = "screens.waiting"


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


def waiting_group() -> str:
    """Group holding every screen with no competition to show.

    A screen whose classroom is not in the running round keeps this socket open
    instead of reconnecting every few seconds to ask again. The Screen ID limit
    is counted per client address, and every screen in a school can share one,
    so unbounded retrying would lock the building out between lessons - and the
    screen would show "reconnecting" for what is really just an idle room.
    """
    return WAITING_GROUP
