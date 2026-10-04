"""Interactive screen management through the administrator interface."""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from classrooms.models import Classroom
from core.tests.factories import create_administrator, create_teacher
from screens.models import InteractiveScreen

CHANGELIST = "/admin/screens/interactivescreen/"


class ScreenAdminPermissionTests(TestCase):
    def test_an_administrator_may_open_the_changelist(self) -> None:
        self.client.force_login(create_administrator())

        self.assertEqual(self.client.get(CHANGELIST).status_code, 200)

    def test_an_anonymous_visitor_is_refused(self) -> None:
        response = self.client.get(CHANGELIST)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_a_teacher_is_refused(self) -> None:
        self.client.force_login(create_teacher())

        response = self.client.get(CHANGELIST)

        self.assertEqual(response.status_code, 302)


class ScreenCrudTests(TestCase):
    def setUp(self) -> None:
        self.client.force_login(create_administrator())
        self.classroom = Classroom.objects.create(name="Science Lab 1")

    def test_create_generates_a_screen_id(self) -> None:
        response = self.client.post(
            reverse("admin:screens_interactivescreen_add"),
            {
                "name": "Front interactive board",
                "classroom": str(self.classroom.pk),
                "is_primary": "on",
                "active": "on",
                "screen_id": "",
                "last_seen": "",
                "ip_address": "",
                "browser_user_agent": "",
                "registration_url_display": "",
                "created_at": "",
                "updated_at": "",
            },
        )

        self.assertEqual(response.status_code, 302)

        screen = InteractiveScreen.objects.get(name="Front interactive board")
        self.assertTrue(screen.screen_id.startswith("AM-"))
        self.assertEqual(screen.classroom, self.classroom)
        self.assertTrue(screen.is_primary)

    def test_a_submitted_screen_id_is_ignored_in_favour_of_a_fresh_one(self) -> None:
        """The Screen ID is generated, so an operator cannot mint a chosen one."""
        InteractiveScreen.objects.create(name="First", screen_id="AM-DUP123")

        response = self.client.post(
            reverse("admin:screens_interactivescreen_add"),
            {"name": "Second", "active": "on", "screen_id": "AM-DUP123"},
        )

        self.assertEqual(response.status_code, 302)
        second = InteractiveScreen.objects.get(name="Second")
        self.assertNotEqual(second.screen_id, "AM-DUP123")
        self.assertTrue(second.screen_id.startswith("AM-"))
        self.assertEqual(InteractiveScreen.objects.count(), 2)

    def test_the_screen_id_cannot_be_edited_by_hand(self) -> None:
        """The Screen ID is the screen's permanent identity."""
        screen = InteractiveScreen.objects.create(name="Front board")
        original_id = screen.screen_id

        self.client.post(
            reverse("admin:screens_interactivescreen_change", args=[screen.pk]),
            {
                "name": "Renamed board",
                "active": "on",
                "screen_id": "AM-CHANGE",
                "last_seen": "",
                "ip_address": "",
                "browser_user_agent": "",
                "registration_url_display": "",
                "created_at": "",
                "updated_at": "",
            },
        )

        screen.refresh_from_db()
        self.assertEqual(screen.screen_id, original_id)
        self.assertEqual(screen.name, "Renamed board")

    def test_the_observed_address_cannot_be_typed_in_by_hand(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front board")
        screen.record_heartbeat(ip_address="10.0.0.5")
        last_seen = screen.last_seen

        self.client.post(
            reverse("admin:screens_interactivescreen_change", args=[screen.pk]),
            {
                "name": "Front board",
                "active": "on",
                "screen_id": screen.screen_id,
                "last_seen": "1999-01-01 00:00:00",
                "ip_address": "1.2.3.4",
                "browser_user_agent": "Forged",
                "registration_url_display": "",
                "created_at": "",
                "updated_at": "",
            },
        )

        screen.refresh_from_db()
        self.assertEqual(screen.ip_address, "10.0.0.5")
        self.assertEqual(screen.browser_user_agent, "")
        self.assertEqual(screen.last_seen, last_seen)

    def test_update_the_classroom(self) -> None:
        elsewhere = Classroom.objects.create(name="Library")
        screen = InteractiveScreen.objects.create(name="Front", classroom=self.classroom)

        self.client.post(
            reverse("admin:screens_interactivescreen_change", args=[screen.pk]),
            {
                "name": "Front",
                "classroom": str(elsewhere.pk),
                "active": "on",
                "screen_id": screen.screen_id,
                "last_seen": "",
                "ip_address": "",
                "browser_user_agent": "",
                "registration_url_display": "",
                "created_at": "",
                "updated_at": "",
            },
        )

        screen.refresh_from_db()
        self.assertEqual(screen.classroom, elsewhere)

    def test_deactivate_rather_than_delete(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front")

        self.client.post(
            reverse("admin:screens_interactivescreen_change", args=[screen.pk]),
            {
                "name": "Front",
                "active": "",
                "screen_id": screen.screen_id,
                "last_seen": "",
                "ip_address": "",
                "browser_user_agent": "",
                "registration_url_display": "",
                "created_at": "",
                "updated_at": "",
            },
        )

        screen.refresh_from_db()
        self.assertFalse(screen.active)

    def test_delete(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front")

        response = self.client.post(
            reverse("admin:screens_interactivescreen_delete", args=[screen.pk]),
            {"post": "yes"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(InteractiveScreen.objects.count(), 0)

    def test_the_changelist_can_be_searched_by_screen_id(self) -> None:
        target = InteractiveScreen.objects.create(name="Findable")
        InteractiveScreen.objects.create(name="Other")

        response = self.client.get(CHANGELIST, {"q": target.screen_id})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["cl"].result_list), 1)

    def test_the_changelist_shows_the_registration_url(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front")

        response = self.client.get(
            reverse("admin:screens_interactivescreen_change", args=[screen.pk])
        )

        self.assertContains(response, screen.registration_path)


class ScreenStatusInAdminTests(TestCase):
    def setUp(self) -> None:
        self.client.force_login(create_administrator())

    def test_an_online_screen_is_reported_as_online(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front")
        screen.record_heartbeat()

        response = self.client.get(CHANGELIST)

        self.assertContains(response, "Online")

    def test_a_screen_that_has_never_been_seen_is_reported_as_offline(self) -> None:
        InteractiveScreen.objects.create(name="Front")

        response = self.client.get(CHANGELIST)

        self.assertContains(response, "Offline")

    def test_a_long_absent_screen_is_reported_as_offline(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front")
        screen.record_heartbeat(seen_at=timezone.now() - timedelta(hours=1))

        response = self.client.get(CHANGELIST)

        self.assertContains(response, "Offline")