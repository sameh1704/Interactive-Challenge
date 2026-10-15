"""Context made available to every template.

The theme is the only thing here. It is a context processor because both base
templates - the shared ``core/base.html`` and the teacher workspace's
``teacher/base.html`` - render the ``<html data-theme>`` attribute and the
switcher form, and a page that extended either of them without being about
themes would otherwise have to remember to pass the value down. Nothing else in
the project needs it, and a processor is the one mechanism that reaches every
render without touching a single view.
"""

from __future__ import annotations

from core import theme


def theme_context(request) -> dict[str, object]:
    """Expose the active theme and the cookie details the switcher needs."""
    return theme.for_templates(request)
