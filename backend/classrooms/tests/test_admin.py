"""Classroom management through the administrator interface."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from classrooms.models import Classroom
from core.tests.factories import TEST_PASSWORD, create_administrator, create_teacher

CHANGELIST = "/admin/classrooms/classroom/"


class ClassroomAdminPermissionTests(TestCase):
    def setUp(self) -> None:
        self.administrator = create_administrator()

    def test_an_administrator_may_open_the_changelist(self) -> None:
        self.client.force_login(self.administrator)

        self.assertEqual(self.client.get(CHANGELIST).status_code, 200)

    def test_the_add_form_is_available(self) -> None:
        self.client.force_login(self.administrator)

        self.assertEqual(
            self.client.get(reverse("admin:classrooms_classroom_add")).status_code, 200
        )

    def test_an_anonymous_visitor_is_refused(self) -> None:
        response = self.client.get(CHANGELIST)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_a_teacher_is_refused(self) -> None:
        teacher = create_teacher()
        self.client.force_login(teacher)

        response = self.client.get(CHANGELIST)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])


class ClassroomCrudTests(TestCase):
    def setUp(self) -> None:
        self.administrator = create_administrator()
        self.client.force_login(self.administrator)
        self.teacher = create_teacher("assigned.teacher")

    def test_create(self) -> None:
        response = self.client.post(
            reverse("admin:classrooms_classroom_add"),
            {
                "name": "Physics Lab",
                "grade": "Grade 9",
                "section": "B",
                "building": "Science Block",
                "floor": "1",
                "room_number": "112",
                "active": "on",
                "teachers": [str(self.teacher.pk)],
                "created_at": "",
                "updated_at": "",
            },
        )

        self.assertEqual(response.status_code, 302)

        classroom = Classroom.objects.get(name="Physics Lab")
        self.assertEqual(classroom.grade, "Grade 9")
        self.assertEqual(classroom.room_number, "112")
        self.assertEqual(list(classroom.teachers.all()), [self.teacher])

    def test_create_rejects_a_duplicate_name(self) -> None:
        Classroom.objects.create(name="Physics Lab")

        response = self.client.post(
            reverse("admin:classrooms_classroom_add"),
            {"name": "Physics Lab", "active": "on", "created_at": "", "updated_at": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Classroom.objects.filter(name="Physics Lab").count(), 1)

    def test_create_rejects_a_blank_name(self) -> None:
        response = self.client.post(
            reverse("admin:classrooms_classroom_add"),
            {"name": "", "active": "on", "created_at": "", "updated_at": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Classroom.objects.count(), 0)

    def test_read_the_change_page(self) -> None:
        classroom = Classroom.objects.create(name="Library")

        response = self.client.get(
            reverse("admin:classrooms_classroom_change", args=[classroom.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Library")

    def test_update(self) -> None:
        classroom = Classroom.objects.create(name="Library")

        self.client.post(
            reverse("admin:classrooms_classroom_change", args=[classroom.pk]),
            {
                "name": "Main Library",
                "grade": "All grades",
                "section": "",
                "building": "Main",
                "floor": "Ground",
                "room_number": "001",
                "created_at": "",
                "updated_at": "",
            },
        )

        classroom.refresh_from_db()
        self.assertEqual(classroom.name, "Main Library")
        self.assertEqual(classroom.building, "Main")

    def test_update_deactivating_a_classroom(self) -> None:
        classroom = Classroom.objects.create(name="Library")

        self.client.post(
            reverse("admin:classrooms_classroom_change", args=[classroom.pk]),
            {"name": "Library", "created_at": "", "updated_at": ""},
        )

        classroom.refresh_from_db()
        self.assertFalse(classroom.active)

    def test_delete(self) -> None:
        classroom = Classroom.objects.create(name="Library")

        response = self.client.post(
            reverse("admin:classrooms_classroom_delete", args=[classroom.pk]),
            {"post": "yes"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Classroom.objects.count(), 0)

    def test_delete_is_refused_while_a_screen_is_registered(self) -> None:
        from screens.models import InteractiveScreen

        classroom = Classroom.objects.create(name="Library")
        InteractiveScreen.objects.create(name="Front board", classroom=classroom)

        response = self.client.post(
            reverse("admin:classrooms_classroom_delete", args=[classroom.pk]),
            {"post": "yes"},
        )

        # Django re-renders the confirmation page with the ProtectedError rather
        # than deleting, so nothing is lost and the operator is told why.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "protected")
        self.assertTrue(Classroom.objects.filter(pk=classroom.pk).exists())
        self.assertEqual(InteractiveScreen.objects.count(), 1)

    def test_the_changelist_can_be_searched(self) -> None:
        Classroom.objects.create(name="Physics Lab")
        Classroom.objects.create(name="Library")

        response = self.client.get(CHANGELIST, {"q": "Physics"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Physics Lab")
        self.assertNotContains(response, ">Library<")

    def test_the_changelist_can_be_filtered_by_grade(self) -> None:
        Classroom.objects.create(name="Physics Lab", grade="Grade 9")
        Classroom.objects.create(name="Library", grade="Grade 5")

        response = self.client.get(CHANGELIST, {"grade__exact": "Grade 9"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["cl"].result_list), 1)

    def test_the_changelist_shows_the_assigned_teachers(self) -> None:
        classroom = Classroom.objects.create(name="Library")
        classroom.teachers.add(self.teacher)

        response = self.client.get(CHANGELIST)

        self.assertContains(response, self.teacher.get_full_name())

    def test_the_changelist_reports_the_screen_count(self) -> None:
        from django.contrib import admin as django_admin

        from screens.models import InteractiveScreen

        classroom = Classroom.objects.create(name="Library")
        InteractiveScreen.objects.create(name="Front", classroom=classroom)

        changelist = self.client.get(CHANGELIST).context["cl"]
        row = next(row for row in changelist.result_list if row.pk == classroom.pk)

        # screen_count is a ModelAdmin display method, so it is read off the
        # registered admin rather than off the model instance.
        model_admin = django_admin.site._registry[Classroom]

        self.assertEqual(changelist.result_list.count(), 1)
        self.assertEqual(model_admin.screen_count(row), 1)
        self.assertContains(self.client.get(CHANGELIST), "1")