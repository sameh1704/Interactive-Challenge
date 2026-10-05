"""The project's admin app config.

Lives in its own module rather than in ``core.apps`` for a reason that is easy to
trip over.

Django resolves a bare ``"core"`` entry in ``INSTALLED_APPS`` by importing
``core.apps`` and taking the single AppConfig subclass there whose ``default`` is
truthy. ``AdminConfig`` - the class this one subclasses - has ``default = True``,
so *importing it into ``core.apps``* makes it a candidate. With ``CoreConfig`` and
``ChallengeAdminConfig`` also there, ``"core"`` then resolves to ``AdminConfig``,
whose ``name`` is ``django.contrib.admin``, and app loading dies with
``Application labels aren't unique, duplicates: admin``.

Keeping the subclass out of ``core.apps`` removes the candidate entirely. The
config still inherits ``default = False`` so that scanning ``core.admin_config``
for a default - which nothing does - cannot pick it up either.

Listed in ``INSTALLED_APPS`` *in place of* ``django.contrib.admin``: both cannot
be present, because this config carries the same ``name``.
"""

from __future__ import annotations

from django.contrib.admin.apps import AdminConfig


class ChallengeAdminConfig(AdminConfig):
    """``django.contrib.admin``, pointed at the project's own admin site.

    Sets ``default_site`` so that ``/admin/`` is served by
    :class:`core.admin_site.ChallengeAdminSite` instead of the stock one.

    Nothing else changes. The URL, the URL name, every
    ``admin:<app>_<model>_<action>`` name and model autodiscovery are all
    inherited, so existing links - including the ones on the staff dashboard -
    keep working and every ``ModelAdmin`` still registers itself against this
    site. See ``core/admin_site.py`` for what the custom site actually adds.
    """

    default_site = "core.admin_site.ChallengeAdminSite"
    default = False