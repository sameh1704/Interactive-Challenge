"""Role-aware dashboards."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.roles import Role
from classrooms.models import Classroom
from core.tests.factories import create_administrator, create_teacher
from screens.models import InteractiveScreen

UserModel = get_user_model()


class DashboardAccessTests(TestCase):
    def setUp(self) -> None:
        self.administrator = UserModel.objects.create_user(
            username="admin", password="pw-admin-long", role=Role.ADMINISTRATOR
        )
        self.teacher = UserModel.objects.create_user(
            username="teacher", password="pw-teacher-long", role=Role.TEACHER
        )

    def test_anonymous_visitor_is_sent_to_sign_in(self) -> None:
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_anonymous_visitor_is_returned_to_the_dashboard_after_signing_in(self) -> None:
        response = self.client.get(reverse("dashboard"))

        self.assertIn(f"next={reverse('dashboard')}", response["Location"])

    def test_a_teacher_reaches_their_dashboard(self) -> None:
        self.client.force_login(self.teacher)

        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_an_administrator_reaches_their_dashboard(self) -> None:
        self.client.force_login(self.administrator)

        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)


class AdministratorDashboardTests(TestCase):
    def setUp(self) -> None:
        self.administrator = UserModel.objects.create_user(
            username="admin", password="pw-admin-long", role=Role.ADMINISTRATOR
        )
        UserModel.objects.create_user(
            username="teacher.one", password="pw-one-long", role=Role.TEACHER
        )
        UserModel.objects.create_user(
            username="teacher.two", password="pw-two-long", role=Role.TEACHER
        )
        self.retired_teacher = UserModel.objects.create_user(
            username="teacher.retired",
            password="pw-retired-long",
            role=Role.TEACHER,
            is_active=False,
        )

        self.classroom = Classroom.objects.create(name="Science Lab 1", grade="Grade 7")
        self.other_classroom = Classroom.objects.create(name="Library", grade="Grade 7")

        self.online_screen = InteractiveScreen.objects.create(
            name="Front board",
            classroom=self.classroom,
            ip_address="10.0.0.5",
        )
        self.online_screen.record_heartbeat(ip_address="10.0.0.5")
        self.online_screen.refresh_from_db()

        self.offline_screen = InteractiveScreen.objects.create(
            name="Side board",
            classroom=self.classroom,
            ip_address="10.0.0.6",
        )
        self.retired_screen = InteractiveScreen.objects.create(
            name="Old board",
            classroom=self.other_classroom,
            active=False,
        )

        self.client.force_login(self.administrator)
        response = self.client.get(reverse("dashboard"))
        self.stats = response.context["stats"]

    def test_total_classrooms_are_counted(self) -> None:
        self.assertEqual(self.stats["classrooms_total"], 2)

    def test_registered_screens_are_counted_including_retired_ones(self) -> None:
        self.assertEqual(self.stats["screens_registered"], 3)

    def test_active_screens_exclude_retired_ones(self) -> None:
        self.assertEqual(self.stats["screens_active"], 2)

    def test_online_and_offline_screens_are_separated(self) -> None:
        self.assertEqual(self.stats["screens_online"], 1)
        self.assertEqual(self.stats["screens_offline"], 1)

    def test_only_active_teachers_are_counted(self) -> None:
        self.assertEqual(self.stats["teachers"], 2)
        self.assertEqual(self.stats["administrators"], 1)

    def test_the_administrator_sees_every_screen(self) -> None:
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(len(response.context["screens"]), 3)

    def test_the_administrator_sees_the_administration_link(self) -> None:
        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, reverse("admin:index"))


class TeacherDashboardTests(TestCase):
    def setUp(self) -> None:
        self.teacher = UserModel.objects.create_user(
            username="teacher", password="pw-teacher-long", role=Role.TEACHER
        )
        self.other_teacher = UserModel.objects.create_user(
            username="teacher.other", password="pw-other-long", role=Role.TEACHER
        )

        self.mine = Classroom.objects.create(name="My classroom", grade="Grade 5")
        self.theirs = Classroom.objects.create(name="Their classroom", grade="Grade 6")
        self.mine.teachers.add(self.teacher)
        self.theirs.teachers.add(self.other_teacher)

        self.my_screen = InteractiveScreen.objects.create(
            name="My board", classroom=self.mine
        )
        self.my_screen.record_heartbeat()
        self.my_screen.refresh_from_db()

        self.client.force_login(self.teacher)
        response = self.client.get(reverse("dashboard"))
        self.context = response.context

    def test_only_assigned_classrooms_are_listed(self) -> None:
        self.assertEqual([c.name for c in self.context["classrooms"]], ["My classroom"])

    def test_another_teachers_classroom_is_not_visible(self) -> None:
        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "Their classroom")

    def test_a_teacher_sees_only_their_own_screen_count(self) -> None:
        self.assertEqual(self.context["stats"]["screens_total"], 1)
        self.assertEqual(self.context["stats"]["screens_online"], 1)

    def test_a_teacher_is_not_offered_the_administration_link(self) -> None:
        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, reverse("admin:index"))

    def test_a_teacher_with_no_classrooms_is_told_so(self) -> None:
        self.teacher.classrooms.clear()

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.context["stats"]["classrooms_total"], 0)
        self.assertContains(response, "not assigned to any classrooms")


class ClassroomVisibilityQueryTests(TestCase):
    def test_for_user_limits_teachers_to_their_assignments(self) -> None:
        teacher = create_teacher("assigned.teacher")
        mine = Classroom.objects.create(name="Mine")
        other = Classroom.objects.create(name="Other")
        mine.teachers.add(teacher)

        self.assertEqual(list(Classroom.objects.for_user(teacher)), [mine])

    def test_for_user_gives_administrators_everything(self) -> None:
        administrator = create_administrator()
        Classroom.objects.create(name="One")
        Classroom.objects.create(name="Two")

        self.assertEqual(Classroom.objects.for_user(administrator).count(), 2)

    def test_for_user_returns_nothing_for_anonymous_users(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        Classroom.objects.create(name="One")

        self.assertEqual(Classroom.objects.for_user(AnonymousUser()).count(), 0)