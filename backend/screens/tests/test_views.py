"""Screen identification and the heartbeat endpoint.

Also verifies what a screen may and may not reach: a screen is an unauthenticated
device, so the endpoint surface must stay deliberately narrow.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from classrooms.models import Classroom
from core.tests.utils import create_administrator, create_teacher
from screens.models import InteractiveScreen


class ScreenIdentificationTests(TestCase):
    def setUp(self) -> None:
        self.classroom = Classroom.objects.create(name="Science Lab 1", grade="Grade 7")
        self.screen = InteractiveScreen.objects.create(
            name="Front interactive board", classroom=self.classroom
        )

    def test_a_screen_identifies_itself_with_its_screen_id(self) -> None:
        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "screens/status.html")

    def test_the_page_shows_the_screen_and_its_classroom(self) -> None:
        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertContains(response, "Front interactive board")
        self.assertContains(response, self.classroom.display_name)
        self.assertContains(response, self.screen.screen_id)

    def test_a_screen_can_identify_its_classroom(self) -> None:
        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.context["classroom"], self.classroom)
        self.assertEqual(response.context["screen"], self.screen)

    def test_an_unknown_screen_id_is_refused(self) -> None:
        response = self.client.get(reverse("screens:status"), {"screen_id": "AM-ZZZZZZ"})

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "screens/unregistered.html")

    def test_a_missing_screen_id_is_refused(self) -> None:
        self.assertEqual(self.client.get(reverse("screens:status")).status_code, 404)

    def test_a_retired_screen_is_refused(self) -> None:
        self.screen.active = False
        self.screen.save(update_fields=["active"])

        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.status_code, 404)

    def test_an_unassigned_screen_still_identifies_itself(self) -> None:
        spare = InteractiveScreen.objects.create(name="Spare board")

        response = self.client.get(
            reverse("screens:status"), {"screen_id": spare.screen_id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not yet assigned to a classroom")

    def test_an_unknown_and_a_retired_screen_look_identical(self) -> None:
        """The endpoint must not reveal which Screen IDs exist."""
        retired = InteractiveScreen.objects.create(name="Retired", active=False)
        unknown = self.client.get(reverse("screens:status"), {"screen_id": "AM-ZZZZZZ"})
        known = self.client.get(
            reverse("screens:status"), {"screen_id": retired.screen_id}
        )

        self.assertEqual(unknown.status_code, known.status_code)
        self.assertTemplateUsed(unknown, "screens/unregistered.html")
        self.assertTemplateUsed(known, "screens/unregistered.html")

    def test_the_screen_id_is_matched_case_insensitively(self) -> None:
        response = self.client.get(
            reverse("screens:status"),
            {"screen_id": self.screen.screen_id.lower()},
        )

        self.assertEqual(response.status_code, 200)

    def test_no_sign_in_is_required_to_open_the_screen_page(self) -> None:
        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.status_code, 200)


class ScreenPageDoesNotLeakAdministrationTests(TestCase):
    """A screen must not be able to reach staff or other screens."""

    def setUp(self) -> None:
        # A distinctive username: the unregistered page legitimately contains the
        # ordinary English word "administrator", which would otherwise collide
        # with the default factory username and pass as a false positive.
        self.administrator = create_administrator(username="chief.admin")
        self.teacher = create_teacher(username="instructor.two")
        self.classroom = Classroom.objects.create(name="Science Lab 1")
        self.screen = InteractiveScreen.objects.create(
            name="Front board", classroom=self.classroom
        )
        self.other_screen = InteractiveScreen.objects.create(
            name="Secret board", classroom=self.classroom
        )

    def _status_response(self):
        return self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

    def test_no_other_screen_is_named_on_the_page(self) -> None:
        self.assertNotContains(self._status_response(), "Secret board")
        self.assertNotContains(self._status_response(), self.other_screen.screen_id)

    def test_no_staff_account_is_named_on_the_page(self) -> None:
        response = self._status_response()

        self.assertNotContains(response, self.administrator.username)
        self.assertNotContains(response, self.teacher.username)

    def test_no_administration_link_is_offered(self) -> None:
        response = self._status_response()

        self.assertNotContains(response, "/admin/")
        self.assertNotContains(response, reverse("screens:heartbeat") + "?screen_id=")

    def test_the_unregistered_page_leaks_nothing_either(self) -> None:
        response = self.client.get(reverse("screens:status"), {"screen_id": "AM-ZZZZZZ"})

        # This page is a 404, so the "not present" check has to allow that status.
        self.assertNotContains(response, self.screen.screen_id, status_code=404)
        self.assertNotContains(response, self.administrator.username, status_code=404)

    def test_a_screen_cannot_reach_the_admin_site(self) -> None:
        response = self.client.get("/admin/screens/interactivescreen/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])


class HeartbeatEndpointTests(TestCase):
    def setUp(self) -> None:
        self.classroom = Classroom.objects.create(name="Science Lab 1")
        self.screen = InteractiveScreen.objects.create(
            name="Front board", classroom=self.classroom
        )

    def _post(self, screen_id: str | None = None, data: dict | None = None, **environ):
        """POST a heartbeat.

        ``data`` becomes part of the submitted form body (used to prove that a
        screen cannot smuggle extra fields). Keyword arguments become WSGI
        environ values, which is how the test client simulates the network and
        browser the screen is really running on.
        """
        payload = {"screen_id": screen_id or self.screen.screen_id}
        payload.update(data or {})
        return self.client.post(reverse("screens:heartbeat"), payload, **environ)

    def test_a_heartbeat_returns_the_screen_status(self) -> None:
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["registered"], True)
        self.assertEqual(response.json()["screen_id"], self.screen.screen_id)
        self.assertEqual(response.json()["status"], "online")

    def test_a_heartbeat_records_last_seen(self) -> None:
        self.assertIsNone(self.screen.last_seen)

        self._post()

        self.screen.refresh_from_db()
        self.assertIsNotNone(self.screen.last_seen)

    def test_a_heartbeat_records_the_observed_address(self) -> None:
        self._post(REMOTE_ADDR="10.0.0.7")

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.ip_address, "10.0.0.7")

    def test_a_heartbeat_records_the_observed_browser(self) -> None:
        self._post(HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64)")

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.browser_user_agent, "Mozilla/5.0 (X11; Linux x86_64)")

    def test_a_heartbeat_reports_the_classroom_to_the_screen(self) -> None:
        self.assertEqual(self._post().json()["classroom"], self.classroom.display_name)

    def test_an_unknown_screen_id_is_refused(self) -> None:
        response = self._post(screen_id="AM-ZZZZZZ")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["registered"], False)

    def test_a_retired_screen_is_refused(self) -> None:
        self.screen.active = False
        self.screen.save(update_fields=["active"])

        self.assertEqual(self._post().status_code, 404)

    def test_a_heartbeat_requires_a_post(self) -> None:
        response = self.client.get(reverse("screens:heartbeat"))

        self.assertEqual(response.status_code, 405)

    def test_a_heartbeat_cannot_change_the_screen_identity_or_classroom(self) -> None:
        """A screen reports presence only; it cannot reconfigure itself."""
        original_id = self.screen.screen_id
        elsewhere = Classroom.objects.create(name="Library")

        self._post(
            data={
                "name": "Renamed by the screen",
                "classroom": elsewhere.pk,
                "active": "false",
                "is_primary": "true",
                "last_seen": "1999-01-01T00:00:00Z",
            },
        )

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.screen_id, original_id)
        self.assertEqual(self.screen.name, "Front board")
        self.assertEqual(self.screen.classroom, self.classroom)
        self.assertTrue(self.screen.active)
        self.assertFalse(self.screen.is_primary)

    @override_settings(SCREEN_TRUST_FORWARDED_FOR=True)
    def test_forwarded_address_is_used_when_explicitly_trusted(self) -> None:
        self._post(
            REMOTE_ADDR="10.0.0.7",
            HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1",
        )

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.ip_address, "203.0.113.9")

    @override_settings(SCREEN_TRUST_FORWARDED_FOR=False)
    def test_forwarded_address_is_ignored_when_not_trusted(self) -> None:
        """An attacker can set X-Forwarded-For, so it must not be believed."""
        self._post(REMOTE_ADDR="10.0.0.7", HTTP_X_FORWARDED_FOR="1.2.3.4")

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.ip_address, "10.0.0.7")

    @override_settings(SCREEN_TRUST_FORWARDED_FOR=True)
    def test_an_untrustworthy_forwarded_address_is_discarded(self) -> None:
        self._post(REMOTE_ADDR="10.0.0.7", HTTP_X_FORWARDED_FOR="not-an-address")

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.ip_address, "10.0.0.7")

    @override_settings(SCREEN_ONLINE_WINDOW_SECONDS=90)
    def test_repeated_heartbeats_keep_the_screen_online(self) -> None:
        self.screen.record_heartbeat(
            seen_at=timezone.now() - timedelta(seconds=120)
        )
        self.screen.refresh_from_db()
        self.assertEqual(self.screen.status, "offline")

        self._post()

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.status, "online")

    def test_the_heartbeat_response_exposes_no_staff_or_other_screens(self) -> None:
        administrator = create_administrator(username="chief.admin")
        other = InteractiveScreen.objects.create(name="Secret board")

        body = self._post().content.decode()

        self.assertNotIn(administrator.username, body)
        self.assertNotIn(other.screen_id, body)
        self.assertNotIn("Secret board", body)


class ScreenUrlTests(TestCase):
    def test_the_screen_page_is_mounted_at_the_documented_path(self) -> None:
        self.assertEqual(reverse("screens:status"), "/screen/")

    def test_the_heartbeat_endpoint_is_under_the_screen_path(self) -> None:
        self.assertEqual(reverse("screens:heartbeat"), "/screen/heartbeat/")