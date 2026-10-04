"""Authentication form."""

from __future__ import annotations

import logging

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from accounts.models import User

logger = logging.getLogger(__name__)


class SignInForm(AuthenticationForm):
    """Sign-in form for school staff.

    The labels are adjusted for a school audience, and the username field points
    at ``User.USERNAME_FIELD`` so the form keeps working if the sign-in key ever
    changes.

    Credentials are counted before they are checked, so a wrong password and an
    unknown username cost the caller the same and both are rate limited per
    username and per client address. Without that, a script gets unlimited
    guesses at the one thing protecting every teacher function.
    """

    username = forms.CharField(
        label="Username",
        max_length=User._meta.get_field(User.USERNAME_FIELD).max_length,
        widget=forms.TextInput(
            attrs={"autofocus": True, "autocomplete": "username"}
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": (
            "That username and password combination is not recognised, or the "
            "account is inactive."
        ),
        "inactive": "This account is inactive. Please contact an administrator.",
        # Deliberately identical to invalid_login: telling a caller that they are
        # being rate limited confirms the address is being counted, and the
        # message carries no information they did not already have.
        "rate_limited": (
            "That username and password combination is not recognised, or the "
            "account is inactive."
        ),
    }

    def clean(self):
        from core.ratelimit import login_allowed
        from screens.network import client_ip

        address = client_ip(self.request) if hasattr(self, "request") else None

        # The username is read by its *name*. `self.username_field` is the model's
        # field object - `User._meta.get_field(User.USERNAME_FIELD)` - not a
        # string, so indexing `self.data` with it silently yields "" and every
        # account shares one counter. That would cap the whole school at
        # LOGIN_RATE_LIMIT sign-ins per window, and leave the audit log below
        # unable to say which account was targeted.
        username = (self.data.get(User.USERNAME_FIELD) or "").strip()

        # Counted first, and unconditionally: whether this attempt would have
        # succeeded is irrelevant to whether it was a guess.
        if not login_allowed(username, address):
            logger.warning(
                "Sign-in refused: rate limit reached for %r from %s.",
                username,
                address or "unknown",
            )
            raise forms.ValidationError(
                self.error_messages["rate_limited"], code="rate_limited"
            )

        cleaned = super().clean()

        if self.errors.get("__all__"):
            # The audit trail for a failed sign-in. The username and the source
            # address are recorded because they are what an administrator needs
            # to see a pattern; the password is never touched, here or anywhere
            # else. A wrong password and an unknown username produce one identical
            # line, so the log cannot be used to enumerate accounts either.
            logger.warning(
                "Failed sign-in for %r from %s.",
                username,
                address or "unknown",
            )

        return cleaned
