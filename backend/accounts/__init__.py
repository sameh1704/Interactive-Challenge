"""School staff identities and roles.

See :mod:`accounts.models` for the rationale behind the custom user model, and
for how directory (AD/LDAP) authentication is intended to be added later
without rewriting any business logic.

The module intentionally imports nothing: importing models from an app package's
``__init__`` triggers app-loading problems during ``django.setup()``.
"""