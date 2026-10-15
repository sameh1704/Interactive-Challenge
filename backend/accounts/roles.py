"""Role definitions for school staff accounts.

Kept in its own module, free of any Django model imports, so that both
:mod:`accounts.models` and :mod:`accounts.managers` can depend on it without a
circular import.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    """What a staff account is allowed to do.

    ``administrator`` manages classrooms, screens and accounts. ``teacher`` is
    the ordinary classroom staff account.

    Values are stored as short lowercase slugs; labels are what operators see.
    Adding a role later is a data change only - no business logic needs to know
    the full set.

    The labels are translated lazily because they are shown in the teacher's own
    dashboard and in the admin, which is what makes them shared interface text
    rather than model detail.
    """

    ADMINISTRATOR = "administrator", _("Administrator")
    TEACHER = "teacher", _("Teacher")
    STAGE_DEPUTY = "stage_deputy", _("Stage Deputy")
