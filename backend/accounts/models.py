"""The custom user model.

Why a custom user model
-----------------------
Roles are a first-class property of a school staff account, so they belong on the
user row. A separate profile table would mean a join on every permission
decision and two sources of truth about what a person is. Swapping
``AUTH_USER_MODEL`` once real data exists is a disruptive migration, so doing it
now - while the database is empty - costs nothing.

Future AD/LDAP authentication
-----------------------------
Directory authentication integrates by *mapping directory attributes onto this
model*, not by replacing it. A custom authentication backend would:

1. bind to the directory and verify the credentials,
2. look the account up by ``USERNAME_FIELD`` (``username``),
3. create or update the local row, keeping ``role``, classroom assignments and
   everything else intact.

Because the rest of the system only ever asks "is this user an administrator or
a teacher, and which classrooms are they assigned to?", no business logic has to
change when that backend is added. Keeping the role in local storage rather than
reading it from the directory is what makes that safe: a directory outage or a
mistyped group mapping cannot silently grant or revoke school-wide access.
"""

from __future__ import annotations

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.validators import MinLengthValidator
from django.db import models
from django.utils import timezone

from accounts.managers import UserManager
from accounts.roles import Role


class User(AbstractBaseUser, PermissionsMixin):
    """A member of school staff who can sign in."""

    username = models.CharField(
        max_length=150,
        unique=True,
        validators=[UnicodeUsernameValidator(), MinLengthValidator(3)],
        help_text="Sign-in name. Also the key used to match a directory account.",
    )
    email = models.EmailField(
        blank=True,
        help_text="Optional. Kept locally so that an account still works when "
        "the directory service is unavailable.",
    )
    full_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Shown instead of the username where space allows.",
    )
    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.TEACHER,
        help_text="Administrators manage classrooms, screens and accounts.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive accounts cannot sign in. Their history is preserved.",
    )
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"
        ordering = ["username"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=list(Role.values)),
                name="accounts_user_role_is_valid",
            )
        ]

    def __str__(self) -> str:
        return self.username

    # -- role helpers ------------------------------------------------------

    @property
    def is_administrator(self) -> bool:
        return self.role == Role.ADMINISTRATOR

    @property
    def is_teacher(self) -> bool:
        return self.role == Role.TEACHER

    @property
    def is_staff(self) -> bool:
        """Grants access to the Django admin site.

        Derived from the role so that there is a single source of truth: an
        account cannot be a superuser in one place and a non-staff user in
        another.
        """
        return self.is_administrator

    # -- display helpers ---------------------------------------------------

    def get_full_name(self) -> str:
        return self.full_name or self.username

    def get_short_name(self) -> str:
        if self.full_name:
            return self.full_name.split()[0]
        return self.username

    def get_initials(self) -> str:
        parts = [part for part in self.full_name.split() if part]
        if not parts:
            return self.username[:2].upper()
        return "".join(part[0] for part in parts[:2]).upper()