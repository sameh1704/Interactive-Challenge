"""The container entrypoint helper behaves as documented."""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from core.management.commands import wait_for_services

OK = {"status": "ok"}
DOWN = {"status": "unavailable", "reason": "not reachable"}


class WaitForServicesTests(SimpleTestCase):
    def test_returns_immediately_when_all_services_are_ready(self) -> None:
        out = StringIO()

        with mock.patch.object(wait_for_services, "CHECKS", (("database", lambda: OK),)):
            call_command("wait_for_services", "--timeout", "5", stdout=out)

        self.assertIn("All services are ready.", out.getvalue())

    def test_retries_then_succeeds(self) -> None:
        attempts = {"count": 0}

        def flaky_check() -> dict:
            attempts["count"] += 1
            return DOWN if attempts["count"] < 3 else OK

        out = StringIO()
        with mock.patch.object(wait_for_services, "CHECKS", (("database", flaky_check),)):
            call_command(
                "wait_for_services", "--timeout", "5", "--interval", "0", stdout=out
            )

        self.assertEqual(attempts["count"], 3)
        self.assertIn("Waiting for database...", out.getvalue())

    def test_raises_when_the_timeout_expires(self) -> None:
        out = StringIO()

        with mock.patch.object(wait_for_services, "CHECKS", (("redis", lambda: DOWN),)):
            with self.assertRaises(CommandError):
                call_command(
                    "wait_for_services",
                    "--timeout",
                    "0",
                    "--interval",
                    "0",
                    stdout=out,
                )


class WaitForServicesIntegrationTests(TestCase):
    """Runs the unmodified checks the entrypoint and health view rely on.

    Declared as ``TestCase`` rather than ``SimpleTestCase`` because the database
    check issues a real query, which ``SimpleTestCase`` forbids.
    """

    def test_real_checks_pass_against_the_docker_services(self) -> None:
        out = StringIO()

        call_command("wait_for_services", "--timeout", "10", stdout=out)

        self.assertIn("All services are ready.", out.getvalue())

    def test_health_checks_report_ok(self) -> None:
        from core.health import check_database, check_redis

        self.assertEqual(check_database()["status"], "ok")
        self.assertEqual(check_redis()["status"], "ok")