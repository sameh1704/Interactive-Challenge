"""PostgreSQL is configured, reachable and used by the application."""

from __future__ import annotations

import os

from django.conf import settings
from django.db import connection
from django.test import TestCase


class DatabaseConfigurationTests(TestCase):
    def test_postgresql_is_the_configured_backend(self) -> None:
        self.assertEqual(
            settings.DATABASES["default"]["ENGINE"], "django.db.backends.postgresql"
        )

    def test_credentials_come_from_the_environment(self) -> None:
        """No credential may be baked into the source tree."""
        database = settings.DATABASES["default"]

        # NAME is excluded here because the test runner rewrites it to the test
        # database; it is asserted separately in
        # DatabaseConnectionTests.test_suite_runs_against_a_separate_test_database.
        for setting, variable in (
            ("USER", "DATABASE_USER"),
            ("PASSWORD", "DATABASE_PASSWORD"),
            ("HOST", "DATABASE_HOST"),
            ("PORT", "DATABASE_PORT"),
        ):
            self.assertEqual(
                str(database[setting]),
                os.environ.get(variable, ""),
                f"{setting} must be read from {variable}.",
            )

    def test_application_uses_postgresql(self) -> None:
        self.assertEqual(connection.vendor, "postgresql")

    def test_only_the_default_alias_is_configured(self) -> None:
        """One PostgreSQL server; no accidental secondary database."""
        self.assertEqual(list(settings.DATABASES), ["default"])


class DatabaseConnectionTests(TestCase):
    """These tests only pass against a real PostgreSQL server."""

    def test_connection_can_be_opened(self) -> None:
        connection.ensure_connection()

        self.assertIsNotNone(connection.connection)

    def test_trivial_query_returns_the_expected_value(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

            self.assertEqual(cursor.fetchone()[0], 1)

    def test_server_reports_a_reachable_version(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")

            self.assertTrue(cursor.fetchone()[0])

    def test_a_failed_statement_does_not_poison_the_connection(self) -> None:
        """Errors must be recoverable through the surrounding savepoint.

        PostgreSQL aborts the whole transaction when a statement fails, so the
        inner atomic() block is what restores a usable connection. Without it,
        every later query in the test would fail too.
        """
        from django.db import DatabaseError, transaction

        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT * FROM table_that_does_not_exist")

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

            self.assertEqual(cursor.fetchone()[0], 1)

    def test_suite_runs_against_a_separate_test_database(self) -> None:
        """The test runner must never touch the live competition database."""
        self.assertTrue(connection.settings_dict["NAME"])
        self.assertNotEqual(
            connection.settings_dict["NAME"],
            os.environ.get("DATABASE_NAME", ""),
            msg="Tests must run against a separate test database.",
        )