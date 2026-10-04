"""Role-based access helpers.

All authorisation decisions go through these helpers so that the rules live in
one place and can be tested directly, rather than being re-implemented as
``user.role == "..."`` comparisons scattered across views.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse

from accounts.roles import Role


def is_authenticated_staff(user) -> bool:
    """True for any signed-in, active member of staff."""
    return bool(user and user.is_authenticated and user.is_active)


def is_administrator(user) -> bool:
    """True only for a signed-in, active administrator."""
    return is_authenticated_staff(user) and user.role == Role.ADMINISTRATOR


def is_teacher(user) -> bool:
    """True only for a signed-in, active teacher."""
    return is_authenticated_staff(user) and user.role == Role.TEACHER


def is_staff_member(user) -> bool:
    """True for either role."""
    return is_authenticated_staff(user) and user.role in set(Role.values)


class RoleRequiredMixin:
    """View mixin restricting access to specific roles.

    Usage::

        class SomeView(LoginRequiredMixin, RoleRequiredMixin, TemplateView):
            allowed_roles = (Role.ADMINISTRATOR,)
    """

    allowed_roles: tuple[str, ...] = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_anonymous(request)
        if not is_staff_member(request.user):
            raise PermissionDenied("This account has no assigned role.")
        if self.allowed_roles and request.user.role not in self.allowed_roles:
            raise PermissionDenied("Your role does not allow this page.")
        return super().dispatch(request, *args, **kwargs)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return self.handle_anonymous(self.request)
        return super().handle_no_permission()

    @staticmethod
    def handle_anonymous(request):
        """Send anonymous visitors to the sign-in page, then back here."""
        return redirect(f"{reverse('accounts:login')}?next={request.get_full_path()}")


def role_required(*roles: str) -> Callable:
    """Decorator restricting a function view to the given roles."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(
                    f"{reverse('accounts:login')}?next={request.get_full_path()}"
                )
            if not is_staff_member(request.user):
                raise PermissionDenied("This account has no assigned role.")
            if roles and request.user.role not in roles:
                raise PermissionDenied("Your role does not allow this page.")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator