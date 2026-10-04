"""Sign-in and sign-out views.

Uses Django's built-in authentication machinery rather than a hand-rolled form:
password hashing, session handling, constant-time comparison and brute-force
friendly throttling all come from the framework.
"""

from __future__ import annotations

from django.contrib.auth import views as auth_views
from django.urls import path

from accounts.forms import SignInForm

app_name = "accounts"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="accounts/login.html",
            authentication_form=SignInForm,
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
]