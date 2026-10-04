"""The InteractiveScreen model, with emphasis on Screen ID being the identity."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from classrooms.models import Classroom
from screens.models import (
    SCREEN_ID_ALPHABET,
    InteractiveScreen,
    ScreenStatus,
    generate_screen_id,
)

UserModel = get_user_model()


class ScreenIdGenerationTests(TestCase):
    def test_a_generated_id_has_the_expected_shape(self) -> None:
        screen_id = generate_screen_id()

        prefix, _, body = screen_id.partition("-")

        self.assertEqual(prefix, "AM")
        self.assertEqual(len(body), 6)
        self.assertTrue(all(character in SCREEN_ID_ALPHABET for character in body))

    def test_generated_ids_avoid_ambiguous_characters(self) -> None:
        """An ID is read aloud and typed by hand, so 0/O and 1/I are excluded."""
        for character in "01OIL":
            self.assertNotIn(character, SCREEN_ID_ALPHABET)

    def test_generated_ids_are_random(self) -> None:
        generated = {generate_screen_id() for _ in range(200)}

        # A 6-character body over a 30-character alphabet: collisions in 200
        # draws are possible but astronomically unlikely.
        self.assertGreater(len(generated), 190)

    def test_a_new_screen_is_given_an_id_automatically(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front board")

        self.assertTrue(screen.screen_id.startswith("AM-"))
        self.assertEqual(InteractiveScreen.objects.count(), 1)


class ScreenCreationTests(TestCase):
    def setUp(self) -> None:
        self.classroom = Classroom.objects.create(name="Science Lab 1")

    def test_create_a_screen_with_the_documented_fields(self) -> None:
        screen = InteractiveScreen.objects.create(
            name="Front interactive board",
            classroom=self.classroom,
            is_primary=True,
            ip_address="10.0.0.5",
            browser_user_agent="Mozilla/5.0 (X11; Linux x86_64)",
        )

        self.assertEqual(screen.name, "Front interactive board")
        self.assertEqual(screen.classroom, self.classroom)
        self.assertTrue(screen.is_primary)
        self.assertEqual(screen.ip_address, "10.0.0.5")
        self.assertEqual(screen.browser_user_agent, "Mozilla/5.0 (X11; Linux x86_64)")

    def test_a_new_screen_is_active_and_has_never_been_seen(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front board")

        self.assertTrue(screen.active)
        self.assertIsNone(screen.last_seen)
        self.assertIsNotNone(screen.created_at)
        self.assertIsNotNone(screen.updated_at)

    def test_a_screen_may_be_registered_before_it_is_installed(self) -> None:
        screen = InteractiveScreen.objects.create(name="Spare board")

        self.assertIsNone(screen.classroom)
        self.assertFalse(screen.is_primary)

    def test_str_includes_the_screen_id(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front board")

        self.assertIn(screen.screen_id, str(screen))
        self.assertIn("Front board", str(screen))


class UniqueScreenIdTests(TestCase):
    def test_two_screens_cannot_share_a_screen_id(self) -> None:
        InteractiveScreen.objects.create(name="First", screen_id="AM-AAAAAA")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InteractiveScreen.objects.create(name="Second", screen_id="AM-AAAAAA")

    def test_two_screen_ids_differing_only_in_case_are_rejected(self) -> None:
        """Lookup is case-insensitive, so the database must be too."""
        InteractiveScreen.objects.create(name="First", screen_id="AM-ABCDEF")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InteractiveScreen.objects.create(name="Second", screen_id="AM-abcdef")

    def test_many_screens_each_get_a_distinct_id(self) -> None:
        screens = [
            InteractiveScreen.objects.create(name=f"Board {index}")
            for index in range(25)
        ]

        identifiers = [screen.screen_id for screen in screens]

        self.assertEqual(len(set(identifiers)), 25)


class ScreenToClassroomTests(TestCase):
    def setUp(self) -> None:
        self.classroom = Classroom.objects.create(name="Science Lab 1")
        self.other_classroom = Classroom.objects.create(name="Library")

    def test_a_screen_belongs_to_its_classroom(self) -> None:
        screen = InteractiveScreen.objects.create(
            name="Front board", classroom=self.classroom
        )

        self.assertEqual(screen.classroom, self.classroom)
        self.assertEqual(list(self.classroom.screens.all()), [screen])

    def test_each_classroom_lists_its_own_screens(self) -> None:
        mine = InteractiveScreen.objects.create(name="Mine", classroom=self.classroom)
        theirs = InteractiveScreen.objects.create(
            name="Theirs", classroom=self.other_classroom
        )

        self.assertEqual(list(self.classroom.screens.all()), [mine])
        self.assertEqual(list(self.other_classroom.screens.all()), [theirs])

    def test_a_classroom_may_have_several_screens(self) -> None:
        InteractiveScreen.objects.create(name="Front", classroom=self.classroom)
        InteractiveScreen.objects.create(name="Side", classroom=self.classroom)

        self.assertEqual(self.classroom.screens.count(), 2)

    def test_only_one_screen_per_classroom_may_be_primary(self) -> None:
        InteractiveScreen.objects.create(
            name="Front", classroom=self.classroom, is_primary=True
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InteractiveScreen.objects.create(
                    name="Second primary", classroom=self.classroom, is_primary=True
                )

    def test_a_primary_screen_must_belong_to_a_classroom(self) -> None:
        screen = InteractiveScreen(name="Orphan", is_primary=True)

        with self.assertRaises(ValidationError) as context:
            screen.full_clean()

        self.assertIn("is_primary", context.exception.error_dict)

    def test_a_classroom_holding_a_screen_cannot_be_deleted(self) -> None:
        """A competition result must not lose its screen by tidying up a room."""
        InteractiveScreen.objects.create(name="Front", classroom=self.classroom)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.classroom.delete()

    def test_deleting_a_screen_frees_the_classroom(self) -> None:
        screen = InteractiveScreen.objects.create(name="Front", classroom=self.classroom)
        screen.delete()

        self.classroom.delete()

        # other_classroom was never given a screen, so it survives.
        self.assertFalse(
            Classroom.objects.filter(pk=self.classroom.pk).exists()
        )
        self.assertEqual(Classroom.objects.count(), 1)


class ScreenIdentityDoesNotDependOnIpTests(TestCase):
    """The core identity rule, in several forms."""

    def setUp(self) -> None:
        self.classroom = Classroom.objects.create(name="Science Lab 1")
        self.screen = InteractiveScreen.objects.create(
            name="Front board",
            classroom=self.classroom,
            ip_address="10.0.0.5",
        )

    def test_an_ip_address_is_not_unique(self) -> None:
        """Two screens behind one NAT share an address and both stay valid."""
        other = InteractiveScreen.objects.create(
            name="Side board",
            classroom=self.classroom,
            ip_address="10.0.0.5",
        )

        self.assertEqual(other.ip_address, self.screen.ip_address)
        self.assertNotEqual(other.screen_id, self.screen.screen_id)

    def test_a_dhcp_change_leaves_the_identity_and_classroom_intact(self) -> None:
        original_id = self.screen.screen_id
        original_classroom = self.screen.classroom

        self.screen.record_heartbeat(ip_address="10.0.9.99")
        self.screen.refresh_from_db()

        self.assertEqual(self.screen.screen_id, original_id)
        self.assertEqual(self.screen.classroom, original_classroom)
        self.assertEqual(self.screen.ip_address, "10.0.9.99")

    def test_swapping_two_screens_addresses_does_not_swap_their_identity(self) -> None:
        other = InteractiveScreen.objects.create(
            name="Side board",
            classroom=self.classroom,
            ip_address="10.0.0.6",
        )

        self.screen.record_heartbeat(ip_address="10.0.0.6")
        other.record_heartbeat(ip_address="10.0.0.5")
        self.screen.refresh_from_db()
        other.refresh_from_db()

        self.assertEqual(self.screen.name, "Front board")
        self.assertEqual(other.name, "Side board")
        self.assertNotEqual(self.screen.screen_id, other.screen_id)

    def test_an_unassigned_screen_gains_an_address_but_keeps_its_identity(self) -> None:
        screen = InteractiveScreen.objects.create(name="Spare")
        original_id = screen.screen_id

        screen.record_heartbeat(ip_address="192.0.2.10")
        screen.refresh_from_db()

        self.assertEqual(screen.screen_id, original_id)
        self.assertIsNone(screen.classroom)

    def test_a_user_agent_change_never_alters_identity(self) -> None:
        original_id = self.screen.screen_id

        self.screen.record_heartbeat(user_agent="Mozilla/5.0 (Windows NT 10.0)")
        self.screen.refresh_from_db()

        self.assertEqual(self.screen.screen_id, original_id)
        self.assertEqual(self.screen.browser_user_agent, "Mozilla/5.0 (Windows NT 10.0)")

    def test_a_heartbeat_never_moves_a_screen_to_another_classroom(self) -> None:
        elsewhere = Classroom.objects.create(name="Library")

        self.screen.record_heartbeat(ip_address="10.0.9.99")
        self.screen.refresh_from_db()

        self.assertNotEqual(self.screen.classroom, elsewhere)
        self.assertEqual(self.screen.classroom, self.classroom)


class HeartbeatTests(TestCase):
    def setUp(self) -> None:
        self.screen = InteractiveScreen.objects.create(name="Front board")

    def test_a_heartbeat_records_last_seen(self) -> None:
        self.assertIsNone(self.screen.last_seen)

        before = timezone.now()
        self.screen.record_heartbeat(ip_address="10.0.0.5", user_agent="TestAgent")
        self.screen.refresh_from_db()

        self.assertIsNotNone(self.screen.last_seen)
        self.assertGreaterEqual(self.screen.last_seen, before)
        self.assertEqual(self.screen.ip_address, "10.0.0.5")
        self.assertEqual(self.screen.browser_user_agent, "TestAgent")

    def test_a_long_user_agent_is_truncated_to_the_stored_length(self) -> None:
        self.screen.record_heartbeat(user_agent="x" * 900)
        self.screen.refresh_from_db()

        self.assertEqual(len(self.screen.browser_user_agent), 512)

    def test_a_heartbeat_without_an_address_keeps_the_last_known_one(self) -> None:
        self.screen.record_heartbeat(ip_address="10.0.0.5")

        self.screen.record_heartbeat(user_agent="TestAgent")
        self.screen.refresh_from_db()

        self.assertEqual(self.screen.ip_address, "10.0.0.5")

    def test_repeated_heartbeats_advance_last_seen(self) -> None:
        earlier = timezone.now() - timedelta(minutes=5)

        self.screen.record_heartbeat(seen_at=earlier)
        self.screen.refresh_from_db()
        first = self.screen.last_seen

        self.screen.record_heartbeat()
        self.screen.refresh_from_db()

        self.assertGreater(self.screen.last_seen, first)


@override_settings(SCREEN_ONLINE_WINDOW_SECONDS=90)
class ScreenStatusTests(TestCase):
    def setUp(self) -> None:
        self.screen = InteractiveScreen.objects.create(name="Front board")

    def test_a_screen_that_has_never_been_seen_is_offline(self) -> None:
        self.assertFalse(self.screen.is_online)
        self.assertEqual(self.screen.status, ScreenStatus.OFFLINE)

    def test_a_recently_seen_screen_is_online(self) -> None:
        self.screen.record_heartbeat()

        self.assertTrue(self.screen.is_online)
        self.assertEqual(self.screen.status, ScreenStatus.ONLINE)

    def test_a_screen_seen_long_ago_is_offline(self) -> None:
        self.screen.record_heartbeat(
            seen_at=timezone.now() - timedelta(seconds=120)
        )
        self.screen.refresh_from_db()

        self.assertFalse(self.screen.is_online)

    def test_status_display_is_human_readable(self) -> None:
        self.screen.record_heartbeat()
        self.assertEqual(self.screen.status_display, "Online")

        self.screen.record_heartbeat(
            seen_at=timezone.now() - timedelta(seconds=120)
        )
        self.assertEqual(self.screen.status_display, "Offline")

    @override_settings(SCREEN_ONLINE_WINDOW_SECONDS=300)
    def test_the_online_window_is_configurable(self) -> None:
        self.screen.record_heartbeat(
            seen_at=timezone.now() - timedelta(seconds=120)
        )
        self.screen.refresh_from_db()

        self.assertTrue(self.screen.is_online)

    def test_online_queryset_matches_the_property(self) -> None:
        online = InteractiveScreen.objects.create(name="Online")
        online.record_heartbeat()

        stale = InteractiveScreen.objects.create(name="Stale")
        stale.record_heartbeat(seen_at=timezone.now() - timedelta(seconds=600))
        InteractiveScreen.objects.create(name="Never seen")

        self.assertEqual(
            [s.name for s in InteractiveScreen.objects.online()], ["Online"]
        )
        # setUp's "Front board" has never been seen either, so it is offline.
        self.assertCountEqual(
            [s.name for s in InteractiveScreen.objects.offline()],
            ["Stale", "Never seen", "Front board"],
        )

    def test_the_online_queryset_respects_the_configured_window(self) -> None:
        screen = InteractiveScreen.objects.create(name="Recent")
        screen.record_heartbeat(
            seen_at=timezone.now() - timedelta(seconds=120)
        )

        with override_settings(SCREEN_ONLINE_WINDOW_SECONDS=300):
            self.assertEqual(InteractiveScreen.objects.online().count(), 1)
        with override_settings(SCREEN_ONLINE_WINDOW_SECONDS=60):
            self.assertEqual(InteractiveScreen.objects.online().count(), 0)


class RegistrationUrlTests(TestCase):
    def setUp(self) -> None:
        self.screen = InteractiveScreen.objects.create(name="Front board")

    def test_the_registration_path_is_relative_and_carries_the_screen_id(self) -> None:
        self.assertEqual(
            self.screen.registration_path, f"/screen/?screen_id={self.screen.screen_id}"
        )

    def test_no_absolute_url_is_produced_without_a_configured_base_url(self) -> None:
        with override_settings(PUBLIC_BASE_URL=""):
            self.assertIsNone(self.screen.registration_url)

    def test_an_absolute_url_uses_the_configured_base_url(self) -> None:
        with override_settings(PUBLIC_BASE_URL="https://challenge.school.local:8443"):
            self.assertEqual(
                self.screen.registration_url,
                f"https://challenge.school.local:8443/screen/?screen_id={self.screen.screen_id}",
            )

    def test_no_address_is_baked_into_the_model(self) -> None:
        """The application must work on a server with any address."""
        self.assertNotIn("192.168", self.screen.registration_path)
        self.assertNotIn("10.0.", self.screen.registration_path)