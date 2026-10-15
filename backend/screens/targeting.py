"""Which screens a competition will reach.

A competition does not address screens: it addresses *classrooms*, and a screen
belongs to a classroom. Everything a teacher needs to know before starting a
round is therefore derived from that relationship, and this module is the one
place it is derived.

The teacher-facing questions it answers:

* Which classrooms am I running this for?
* Which screens are registered in those classrooms?
* Which of those screens are connected right now?
* Which participating classrooms have no screen at all, so nobody will see it?
* Which screens I am counting on are offline?

The last two are the important ones, and they are counted rather than hidden. A
competition that starts with an offline screen has *not* reached that screen -
the server only writes to a socket that is open, and no socket is open - so the
honest wording is "this screen will miss the round", not "sent".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from screens.models import InteractiveScreen


@dataclass(frozen=True)
class ScreenTarget:
    """One registered screen in a participating classroom."""

    screen: InteractiveScreen

    @property
    def screen_id(self) -> str:
        return self.screen.screen_id

    @property
    def name(self) -> str:
        return self.screen.name

    @property
    def is_online(self) -> bool:
        return self.screen.is_online

    @property
    def status_display(self) -> str:
        return self.screen.status_display


@dataclass(frozen=True)
class ClassroomTarget:
    """A participating classroom and the screens it offers."""

    classroom: object
    screens: tuple[ScreenTarget, ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return getattr(self.classroom, "display_name", None) or str(self.classroom)

    @property
    def online_screens(self) -> tuple[ScreenTarget, ...]:
        return tuple(target for target in self.screens if target.is_online)

    @property
    def offline_screens(self) -> tuple[ScreenTarget, ...]:
        return tuple(target for target in self.screens if not target.is_online)

    @property
    def has_screen(self) -> bool:
        return bool(self.screens)

    @property
    def has_online_screen(self) -> bool:
        return bool(self.online_screens)

    @property
    def status_display(self) -> str:
        """A short, translatable summary of what this classroom will show."""
        if not self.screens:
            return "no-screen"
        if not self.has_online_screen:
            return "screen-offline"
        return "screen-online"


@dataclass(frozen=True)
class Targeting:
    """The whole picture for one competition."""

    competition: object
    classrooms: tuple[ClassroomTarget, ...] = field(default_factory=tuple)

    # -- counts ------------------------------------------------------------

    @property
    def screens(self) -> tuple[ScreenTarget, ...]:
        return tuple(
            screen for target in self.classrooms for screen in target.screens
        )

    @property
    def online_screens(self) -> tuple[ScreenTarget, ...]:
        return tuple(
            screen for screen in self.screens if screen.is_online
        )

    @property
    def offline_screens(self) -> tuple[ScreenTarget, ...]:
        return tuple(
            screen for screen in self.screens if not screen.is_online
        )

    @property
    def classrooms_without_screens(self) -> tuple[ClassroomTarget, ...]:
        return tuple(target for target in self.classrooms if not target.has_screen)

    # -- verdicts ----------------------------------------------------------

    @property
    def has_any_screen(self) -> bool:
        return bool(self.screens)

    @property
    def has_online_screen(self) -> bool:
        return bool(self.online_screens)

    @property
    def classrooms_reached(self) -> tuple[ClassroomTarget, ...]:
        """Classrooms a live round will actually reach right now."""
        return tuple(target for target in self.classrooms if target.has_online_screen)

    @property
    def summary(self) -> str:
        """A short, translatable verdict for the whole competition."""
        if not self.has_any_screen:
            return "no-screens"
        if not self.has_online_screen:
            return "all-offline"
        if self.offline_screens or self.classrooms_without_screens:
            return "partial"
        return "ready"


def for_competition(competition) -> Targeting:
    """Build the targeting picture for ``competition``."""
    return for_ids(
        list(
            competition.classrooms.values_list("classroom_id", flat=True)
        )
    )


def for_ids(classroom_ids) -> Targeting:
    """Build the targeting picture for a set of classrooms.

    Used by the wizard's review step, where no competition exists yet and only
    the chosen classroom ids are known. Same rules, same warnings, one code path.
    """
    ids = [pk for pk in classroom_ids if str(pk).isdigit()]
    screens = (
        InteractiveScreen.objects.active()
        .filter(classroom_id__in=ids)
        .select_related("classroom")
        .order_by("classroom__name", "-is_primary", "name")
    )

    by_classroom: dict[int, list[ScreenTarget]] = {int(pk): [] for pk in ids}
    for screen in screens:
        if screen.classroom_id in by_classroom:
            by_classroom[screen.classroom_id].append(ScreenTarget(screen))

    from classrooms.models import Classroom

    classrooms = Classroom.objects.filter(pk__in=ids).order_by("name")

    return Targeting(
        competition=None,
        classrooms=tuple(
            ClassroomTarget(
                classroom=classroom,
                screens=tuple(by_classroom[classroom.pk]),
            )
            for classroom in classrooms
        ),
    )
