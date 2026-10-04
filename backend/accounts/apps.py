"""Application configuration for accounts."""

from __future__ import annotations

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """School staff identities and their roles."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "Accounts"