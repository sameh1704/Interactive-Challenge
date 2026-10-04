"""The landing page renders and advertises the project correctly."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse


class LandingPageTests(TestCase):
    def test_landing_page_returns_200(self) -> None:
        response = self.client.get(reverse("core:landing"))

        self.assertEqual(response.status_code, 200)

    def test_landing_page_uses_the_landing_template(self) -> None:
        response = self.client.get(reverse("core:landing"))

        self.assertTemplateUsed(response, "core/home.html")

    def test_landing_page_shows_the_project_name(self) -> None:
        response = self.client.get(reverse("core:landing"))

        self.assertContains(response, "Al Manar")
        self.assertContains(response, "Interactive Challenge")

    def test_landing_page_describes_the_project(self) -> None:
        response = self.client.get(reverse("core:landing"))

        body = response.content.decode().lower()
        self.assertIn("school-internal", body)
        self.assertIn("classroom", body)

    def test_root_url_resolves_to_the_landing_page(self) -> None:
        self.assertEqual(reverse("core:landing"), "/")

    def test_landing_page_reports_the_application_version(self) -> None:
        response = self.client.get(reverse("core:landing"))

        self.assertContains(response, "v")