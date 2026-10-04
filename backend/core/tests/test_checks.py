"""The deployment system checks catch a misconfigured environment.

These are what replaced the import-time validation in the settings module, so
they must behave at least as strictly.
"""

from __future__ import annotations

import os
from unittest import mock

from django.core.checks import Error
from django.test import SimpleTestCase

from core.checks import (
    PLACEHOLDER_VALUES,
    REQUIRED_VARIABLES,
    check_allowed_hosts,
    check_required_environment_variables,
)


class RequiredEnvironmentVariableTests(SimpleTestCase):
    def test_a_fully_configured_environment_passes(self) -> None:
        with mock.patch.dict(os.environ, dict(REQUIRED_VARIABLES), clear=False):
            errors = check_required_environment_variables(None)

        self.assertEqual(errors, [])

    def test_a_missing_variable_is_reported(self) -> None:
        # patch.dict without a value only restores on exit; it does not remove
        # keys, so the variable has to be popped explicitly.
        with mock.patch.dict(os.environ):
            os.environ.pop("DATABASE_PASSWORD", None)
            errors = check_required_environment_variables(None)

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Error)
        self.assertIn("DATABASE_PASSWORD", errors[0].msg)
        self.assertEqual(errors[0].id, "challenge.E001")

    def test_an_empty_variable_is_reported(self) -> None:
        with mock.patch.dict(
            os.environ, {**REQUIRED_VARIABLES, "REDIS_HOST": "   "}, clear=False
        ):
            errors = check_required_environment_variables(None)

        self.assertEqual(len(errors), 1)
        self.assertIn("REDIS_HOST", errors[0].msg)

    def test_a_placeholder_value_is_reported(self) -> None:
        placeholder = sorted(PLACEHOLDER_VALUES)[0]
        environment = {**REQUIRED_VARIABLES, "DJANGO_SECRET_KEY": placeholder}

        with mock.patch.dict(os.environ, environment, clear=False):
            errors = check_required_environment_variables(None)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "challenge.E002")

    def test_the_current_environment_satisfies_the_checks(self) -> None:
        """The container is configured, so its own checks must pass."""
        self.assertEqual(check_required_environment_variables(None), [])


class AllowedHostsCheckTests(SimpleTestCase):
    def test_a_wildcard_host_is_reported(self) -> None:
        with mock.patch("core.checks.settings.ALLOWED_HOSTS", ["*"]):
            errors = check_allowed_hosts(None)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "challenge.E003")

    def test_explicit_hosts_pass(self) -> None:
        with mock.patch("core.checks.settings.ALLOWED_HOSTS", ["challenge.local"]):
            errors = check_allowed_hosts(None)

        self.assertEqual(errors, [])

    def test_the_current_hosts_pass(self) -> None:
        self.assertEqual(check_allowed_hosts(None), [])


class RegisteredCheckTests(SimpleTestCase):
    def test_checks_are_registered_with_django(self) -> None:
        from django.core.checks import registry

        # include_deployment_checks is required because check_allowed_hosts is
        # registered with deploy=True and so is skipped by default.
        registered = {
            check.__name__
            for check in registry.registry.get_checks(include_deployment_checks=True)
        }

        self.assertIn("check_required_environment_variables", registered)
        self.assertIn("check_allowed_hosts", registered)

    def test_manage_check_passes_in_this_environment(self) -> None:
        """`manage.py check`, which the container entrypoint runs, must pass."""
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("check", stdout=out)

        self.assertIn("no issues", out.getvalue().lower())