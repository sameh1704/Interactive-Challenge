"""Template helpers for the teacher workspace's wizards."""

from __future__ import annotations

from urllib.parse import urlencode

from django import template
from django.http import QueryDict

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Get an item from a dictionary using a key from the template."""
    try:
        return dictionary.get(key)
    except AttributeError:
        return None


@register.filter
def index_of(list_, value):
    """Return the index of a value in a list."""
    try:
        return list_.index(value)
    except (ValueError, AttributeError):
        return -1


@register.filter
def join_names(targets):
    """Classroom names from a sequence of ``ClassroomTarget``, comma separated.

    Exists so the warning under the review step's screen table can name every
    classroom with no screen without the template reaching into the targeting
    objects itself.
    """
    return ", ".join(target.name for target in targets)


@register.filter
def join_screen_ids(screens):
    """Screen IDs from a sequence of ``ScreenTarget``, comma separated."""
    return ", ".join(screen.screen_id for screen in screens)


@register.simple_tag(takes_context=True)
def querystring(context, **overrides) -> str:
    """The current query string with ``overrides`` applied.

    Pagination links have to carry the filters with them, or clicking "next"
    silently throws away the search, the subject and the type the teacher chose.
    Rebuilding it from ``request.GET`` - rather than from variables the template
    happens to hold - is what keeps every filter on the page in the URL at once,
    which is also what makes the whole view bookmarkable.
    """
    request = context.get("request")
    query: QueryDict = request.GET if request is not None else QueryDict()

    params: dict[str, object] = {}
    for key in query:
        if key == "page" or key in overrides:
            continue
        params[key] = query.getlist(key)
    for key, value in overrides.items():
        if value is not None:
            params[key] = value

    return urlencode(params, doseq=True)
