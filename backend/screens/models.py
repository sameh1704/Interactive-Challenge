"""The InteractiveScreen model.

Identity rules
--------------
``screen_id`` is the **only** identity of a screen. It is generated once at
creation, is unique, and is never derived from anything about the network.

``ip_address`` and ``browser_user_agent`` are technical observations recorded on
every heartbeat so that an administrator can see what is happening on the
network. They are deliberately *not* part of the identity and *not* unique:

* DHCP re-addressing a screen changes ``ip_address`` and nothing else. The screen
  keeps its ``screen_id``, its classroom and its competition history.
* Two screens may legitimately share an address behind NAT, or one screen may
  move between addresses while another stays put.
* If identity were derived from the IP, a DHCP lease change would silently
  reassign a room's participation in a competition to a different device.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinLengthValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

# Ambiguous glyphs (0/O, 1/I/L) are excluded: a Screen ID is read aloud to an
# operator and typed by hand, so it must survive that.
SCREEN_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SCREEN_ID_LENGTH = 6
SCREEN_ID_PREFIX = "AM"


def generate_screen_id() -> str:
    """Return a new, random Screen ID such as ``AM-7KQ4XB``.

    Uses :mod:`secrets` because this value is the sole credential a screen
    presents; a predictable generator would let anyone impersonate a screen.
    """
    body = "".join(secrets.choice(SCREEN_ID_ALPHABET) for _ in range(SCREEN_ID_LENGTH))
    return f"{SCREEN_ID_PREFIX}-{body}"


class ScreenStatus(models.TextChoices):
    """Reported presence of a screen."""

    ONLINE = "online", "Online"
    OFFLINE = "offline", "Offline"


class InteractiveScreenQuerySet(models.QuerySet):
    """Presence-aware lookups."""

    def active(self):
        return self.filter(active=True)

    def online(self):
        """Screens seen within the configured heartbeat window."""
        cutoff = timezone.now() - timedelta(seconds=settings.SCREEN_ONLINE_WINDOW_SECONDS)
        return self.filter(last_seen__gte=cutoff)

    def offline(self):
        cutoff = timezone.now() - timedelta(seconds=settings.SCREEN_ONLINE_WINDOW_SECONDS)
        return self.filter(
            models.Q(last_seen__isnull=True) | models.Q(last_seen__lt=cutoff)
        )


class InteractiveScreen(models.Model):
    """A registered interactive screen in a classroom.

    ``screen_id`` is generated on creation and is not editable, so a Screen ID
    cannot be retyped into a different screen by accident.
    """

    screen_id = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        default=generate_screen_id,
        validators=[MinLengthValidator(9), MaxLengthValidator(16)],
        help_text="Permanent identity of this screen. Generated once, never changed.",
    )
    name = models.CharField(
        max_length=120,
        help_text="Descriptive name, for example 'Front interactive board'.",
    )
    classroom = models.ForeignKey(
        "classrooms.Classroom",
        on_delete=models.PROTECT,
        related_name="screens",
        null=True,
        blank=True,
        help_text=(
            "The classroom this screen belongs to. A screen can be registered "
            "before it is installed; assign it when it is placed."
        ),
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="The main screen for its classroom. At most one per classroom.",
    )
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="Last observed address. Monitoring data only - never identity.",
    )
    browser_user_agent = models.CharField(
        max_length=512,
        blank=True,
        help_text="Last observed browser. Monitoring data only - never identity.",
    )
    last_seen = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="Updated by the heartbeat. Never set by an operator.",
    )
    active = models.BooleanField(
        default=True,
        help_text="Deactivate to retire a screen without losing its history.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = InteractiveScreenQuerySet.as_manager()

    class Meta:
        verbose_name = "interactive screen"
        verbose_name_plural = "interactive screens"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["last_seen"], name="screens_last_seen_idx"),
        ]
        constraints = [
            # A classroom has at most one primary screen. Several other screens
            # may belong to the same classroom later, so this must not be a
            # unique constraint on classroom alone.
            models.UniqueConstraint(
                fields=["classroom"],
                condition=models.Q(is_primary=True, classroom__isnull=False),
                name="screens_one_primary_per_classroom",
            ),
            # Screen IDs are looked up case-insensitively (an operator may type
            # "am-7kq4xb"), so the database must reject two IDs that differ only
            # by case. Without this, "AM-ABCDEF" and "AM-abcdef" could both
            # exist and the lookup would be ambiguous.
            models.UniqueConstraint(
                Lower("screen_id"),
                name="screens_screen_id_unique_ignoring_case",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.screen_id})"

    def clean(self) -> None:
        super().clean()
        if self.is_primary and self.classroom_id is None:
            raise ValidationError(
                {"is_primary": "A primary screen must be assigned to a classroom."}
            )

    def save(self, *args, **kwargs):
        # A Screen ID must never be blank or reused with a different screen.
        if not self.screen_id:
            self.screen_id = generate_screen_id()
        return super().save(*args, **kwargs)

    # -- identity ----------------------------------------------------------

    @property
    def registration_path(self) -> str:
        """Relative URL a screen device opens to identify itself."""
        from django.urls import reverse

        return f"{reverse('screens:status')}?screen_id={self.screen_id}"

    @property
    def registration_url(self) -> str | None:
        """Absolute URL for display to an operator.

        Returns ``None`` when ``PUBLIC_BASE_URL`` is not configured, so that no
        address is ever guessed or hard-coded; the UI then shows the relative
        path and the operator's host instead.
        """
        base_url = settings.PUBLIC_BASE_URL
        if not base_url:
            return None
        return f"{base_url}{self.registration_path}"

    # -- presence ----------------------------------------------------------

    @property
    def is_online(self) -> bool:
        if self.last_seen is None:
            return False
        window = timedelta(seconds=settings.SCREEN_ONLINE_WINDOW_SECONDS)
        return (timezone.now() - self.last_seen) <= window

    @property
    def status(self) -> str:
        return ScreenStatus.ONLINE if self.is_online else ScreenStatus.OFFLINE

    @property
    def status_display(self) -> str:
        return ScreenStatus(self.status).label

    def record_heartbeat(
        self,
        ip_address: str | None = None,
        user_agent: str = "",
        seen_at=None,
    ) -> None:
        """Record that this screen is present.

        Only network observations and ``last_seen`` are written. Identity,
        classroom and every operator-managed field are untouched, so a heartbeat
        can never move a screen to another classroom.
        """
        updates: list[str] = ["last_seen"]

        if ip_address:
            updates.append("ip_address")
        if user_agent:
            updates.append("browser_user_agent")

        self.last_seen = seen_at or timezone.now()
        if ip_address:
            self.ip_address = ip_address
        if user_agent:
            self.browser_user_agent = user_agent[:512]

        # save(update_fields=...) avoids writing unrelated columns and keeps the
        # heartbeat cheap enough to run every few seconds from every screen.
        self.save(update_fields=updates)