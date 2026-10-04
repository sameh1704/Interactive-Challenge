"""The /health/ endpoint reports application and dependency health."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.urls import reverse

HEALTH_URL = "/health/"


class HealthEndpointTests(TestCase):
    def test_health_url_resolves_to_the_documented_path(self) -> None:
        self.assertEqual(reverse("core:health"), HEALTH_URL)

    def test_health_endpoint_returns_200(self) -> None:
        response = self.client.get(HEALTH_URL)

        self.assertEqual(response.status_code, 200)

    def test_health_endpoint_reports_ok_when_all_dependencies_are_up(self) -> None:
        response = self.client.get(HEALTH_URL)
        payload = response.json()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checks"]["database"]["status"], "ok")
        self.assertEqual(payload["checks"]["redis"]["status"], "ok")

    def test_health_endpoint_identifies_the_service_and_version(self) -> None:
        response = self.client.get(HEALTH_URL)
        payload = response.json()

        self.assertEqual(payload["service"], "challenge-web")
        self.assertIn("version", payload)
        self.assertIn("environment", payload)

    def test_health_endpoint_includes_a_timestamp(self) -> None:
        response = self.client.get(HEALTH_URL)
        payload = response.json()

        self.assertIn("timestamp", payload)

    def test_health_endpoint_returns_503_when_the_database_is_down(self) -> None:
        failure = {"status": "unavailable", "reason": "The application database is not reachable."}

        with mock.patch(
            "core.views.run_checks",
            return_value={"database": failure, "redis": {"status": "ok"}},
        ):
            response = self.client.get(HEALTH_URL)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unavailable")

    def test_health_endpoint_returns_503_when_redis_is_down(self) -> None:
        failure = {"status": "unavailable", "reason": "The cache/Redis service is not reachable."}

        with mock.patch(
            "core.views.run_checks",
            return_value={"database": {"status": "ok"}, "redis": failure},
        ):
            response = self.client.get(HEALTH_URL)

        self.assertEqual(response.status_code, 503)

    def test_health_endpoint_does_not_leak_infrastructure_details(self) -> None:
        response = self.client.get(HEALTH_URL)
        body = response.content.decode()

        self.assertNotIn("password", body.lower())
        self.assertNotIn("traceback", body.lower())