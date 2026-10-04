"""Creating classrooms and assigning teachers."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.roles import Role
from classrooms.models import Classroom

UserModel = get_user_model()


class ClassroomCreationTests(TestCase):
    def test_create_a_classroom(self) -> None:
        classroom = Classroom.objects.create(
            name="Science Lab 1",
            grade="Grade 7",
            section="A",
            building="Main",
            floor="2",
            room_number="201",
        )

        self.assertEqual(classroom.name, "Science Lab 1")
        self.assertEqual(classroom.grade, "Grade 7")
        self.assertEqual(classroom.section, "A")
        self.assertEqual(classroom.building, "Main")
        self.assertEqual(classroom.floor, "2")
        self.assertEqual(classroom.room_number, "201")

    def test_a_new_classroom_is_active_and_timestamped(self) -> None:
        classroom = Classroom.objects.create(name="Library")

        self.assertTrue(classroom.active)
        self.assertIsNotNone(classroom.created_at)
        self.assertIsNotNone(classroom.updated_at)

    def test_optional_fields_may_be_left_blank(self) -> None:
        classroom = Classroom.objects.create(name="Annexe")

        self.assertEqual(classroom.grade, "")
        self.assertEqual(classroom.location, "")

    def test_classroom_names_are_unique(self) -> None:
        Classroom.objects.create(name="Science Lab 1")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Classroom.objects.create(name="Science Lab 1")

    def test_classrooms_are_ordered_by_name(self) -> None:
        Classroom.objects.create(name="Zebra room")
        Classroom.objects.create(name="Alpha room")

        self.assertEqual(
            [c.name for c in Classroom.objects.all()], ["Alpha room", "Zebra room"]
        )

    def test_str_is_the_classroom_name(self) -> None:
        self.assertEqual(str(Classroom(name="Library")), "Library")

    def test_active_queryset_excludes_inactive_classrooms(self) -> None:
        Classroom.objects.create(name="In use")
        Classroom.objects.create(name="Closed", active=False)

        self.assertEqual([c.name for c in Classroom.objects.active()], ["In use"])


class TeacherAssignmentTests(TestCase):
    def setUp(self) -> None:
        self.teacher = UserModel.objects.create_user(
            username="teacher", password="pw-long", role=Role.TEACHER
        )
        self.administrator = UserModel.objects.create_user(
            username="admin", password="pw-long", role=Role.ADMINISTRATOR
        )
        self.classroom = Classroom.objects.create(name="Science Lab 1")

    def test_a_classroom_starts_with_no_teachers(self) -> None:
        self.assertEqual(self.classroom.teachers.count(), 0)

    def test_a_teacher_can_be_assigned_to_a_classroom(self) -> None:
        self.classroom.teachers.add(self.teacher)

        self.assertEqual(list(self.classroom.teachers.all()), [self.teacher])

    def test_assignment_is_visible_from_the_teacher_side(self) -> None:
        self.classroom.teachers.add(self.teacher)

        self.assertEqual(list(self.teacher.classrooms.all()), [self.classroom])

    def test_a_teacher_can_hold_several_classrooms(self) -> None:
        second = Classroom.objects.create(name="Library")
        self.classroom.teachers.add(self.teacher)
        second.teachers.add(self.teacher)

        self.assertEqual(self.teacher.classrooms.count(), 2)

    def test_several_teachers_can_share_a_classroom(self) -> None:
        self.classroom.teachers.add(self.teacher, self.administrator)

        self.assertEqual(self.classroom.teachers.count(), 2)

    def test_removing_an_assignment_keeps_the_classroom(self) -> None:
        self.classroom.teachers.add(self.teacher)
        self.classroom.teachers.remove(self.teacher)

        self.assertEqual(self.classroom.teachers.count(), 0)
        self.assertTrue(Classroom.objects.filter(pk=self.classroom.pk).exists())


class DisplayTests(TestCase):
    def test_location_joins_the_parts_it_has(self) -> None:
        classroom = Classroom.objects.create(
            name="Lab", building="Main", floor="2", room_number="201"
        )

        self.assertEqual(classroom.location, "Main, 2, 201")

    def test_display_name_includes_grade_and_section(self) -> None:
        classroom = Classroom.objects.create(
            name="Science Lab", grade="Grade 7", section="A"
        )

        self.assertEqual(classroom.display_name, "Grade 7 - Science Lab (A)")

    def test_display_name_does_not_repeat_a_grade_already_in_the_name(self) -> None:
        classroom = Classroom.objects.create(name="Grade 7 Science Lab", grade="Grade 7")

        self.assertEqual(classroom.display_name, "Grade 7 Science Lab")

    def test_display_name_without_a_grade_is_just_the_name(self) -> None:
        classroom = Classroom.objects.create(name="Library", section="B")

        self.assertEqual(classroom.display_name, "Library (B)")