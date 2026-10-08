from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Get an item from a dictionary using a key from the template."""
    return dictionary.get(key)


@register.filter
def index_of(list_, value):
    """Return the index of a value in a list."""
    try:
        return list_.index(value)
    except ValueError:
        return -1