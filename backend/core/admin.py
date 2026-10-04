"""Shared admin behaviour.

Administrators manage classrooms, screens and staff accounts. Rather than
requiring an operator to tick model permissions for every account, role is the
single source of truth: an administrator manages everything, a teacher manages
nothing.

That matters operationally. Django's default is per-model permissions, which
means a newly added model is invisible to every administrator until somebody
remembers to grant it. During a live competition that is exactly the wrong time
to discover it.
"""

from __future__ import annotations

from accounts.permissions import is_administrator


class AdministratorAdminMixin:
    """Grants full access to administrators and none to anyone else.

    Applied to each ``ModelAdmin``. Adding a model means remembering to mix this
    in, which is a far smaller and more obvious obligation than remembering to
    grant a permission, and it is enforced by
    ``test_admin.py::test_every_model_admin_requires_the_administrator_role``.
    """

    def has_module_permission(self, request) -> bool:
        return is_administrator(request.user)

    def has_view_permission(self, request, obj=None) -> bool:
        return is_administrator(request.user)

    def has_add_permission(self, request) -> bool:
        return is_administrator(request.user)

    def has_change_permission(self, request, obj=None) -> bool:
        return is_administrator(request.user)

    def has_delete_permission(self, request, obj=None) -> bool:
        return is_administrator(request.user)

    def has_view_or_change_permission(self, request, obj=None) -> bool:
        return is_administrator(request.user)