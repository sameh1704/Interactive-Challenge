"""Manager for the custom user model."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.base_user import BaseUserManager

from accounts.roles import Role


class UserManager(BaseUserManager):
    """Creates users and superusers for :class:`accounts.models.User`.

    ``use_in_migrations`` is enabled so that historical data migrations can
    build users without depending on the current manager implementation.
    """

    use_in_migrations = True

    def create_user(
        self, username: str, password: str | None = None, **extra_fields: Any
    ):
        """Create a regular user.

        An unusable password is stored when ``password`` is omitted, which is the
        correct state for an account provisioned by a directory service that has
        never had a local password set.

        Follows the convention of ``django.contrib.auth``'s own manager: field
        validation is the job of forms and database constraints, not of the
        manager. Callers that need validation run ``full_clean()``.
        """
        if not username or not username.strip():
            raise ValueError("A username is required.")

        user = self.model(username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(
        self, username: str, password: str | None = None, **extra_fields: Any
    ):
        """Create a superuser that can also reach the Django admin site.

        The role is forced to ``administrator`` rather than merely defaulted:
        ``is_staff`` is derived from the role, so a superuser left as a teacher
        would be unable to log into the admin site at all.

        ``is_active`` and ``is_superuser`` are only defaulted. A caller that
        explicitly asks for an inactive superuser is making a mistake, so it is
        reported rather than silently corrected.
        """
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields["role"] = Role.ADMINISTRATOR

        if extra_fields.get("is_superuser") is not True:
            raise ValueError("A superuser must have is_superuser=True.")
        if not extra_fields.get("is_active"):
            raise ValueError("A superuser must have is_active=True.")

        return self.create_user(username, password, **extra_fields)