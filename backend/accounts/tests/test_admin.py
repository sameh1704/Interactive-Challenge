"""Staff account management through the administrator interface."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.roles import Role
from core.tests.factories import create_administrator

UserModel = get_user_model()
CHANGELIST = "/admin/accounts/user/"


class UserAdminPermissionTests(TestCase):
    def test_an_administrator_may_manage_accounts(self) -> None:
        self.client.force_login(create_administrator())

        self.assertEqual(self.client.get(CHANGELIST).status_code, 200)

    def test_a_teacher_is_refused(self) -> None:
        from core.tests.factories import create_teacher

        self.client.force_login(create_teacher())

        response = self.client.get(CHANGELIST)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_an_anonymous_visitor_is_refused(self) -> None:
        response = self.client.get(CHANGELIST)

        self.assertEqual(response.status_code, 302)


class UserCrudTests(TestCase):
    def setUp(self) -> None:
        self.client.force_login(create_administrator())

    def test_create_a_teacher(self) -> None:
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "new.teacher",
                "full_name": "New Teacher",
                "email": "new.teacher@school.local",
                "role": Role.TEACHER,
                "password1": "a-long-enough-password",
                "password2": "a-long-enough-password",
            },
        )

        self.assertEqual(response.status_code, 302)

        user = UserModel.objects.get(username="new.teacher")
        self.assertEqual(user.role, Role.TEACHER)
        self.assertTrue(user.check_password("a-long-enough-password"))

    def test_create_an_administrator(self) -> None:
        self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "new.admin",
                "full_name": "New Admin",
                "role": Role.ADMINISTRATOR,
                "password1": "a-long-enough-password",
                "password2": "a-long-enough-password",
            },
        )

        user = UserModel.objects.get(username="new.admin")
        self.assertTrue(user.is_administrator)
        self.assertTrue(user.is_staff)

    def test_creation_rejects_mismatched_passwords(self) -> None:
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "bad.password",
                "role": Role.TEACHER,
                "password1": "a-long-enough-password",
                "password2": "a-different-password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(UserModel.objects.filter(username="bad.password").exists())

    def test_creation_rejects_a_short_username(self) -> None:
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "ab",
                "role": Role.TEACHER,
                "password1": "a-long-enough-password",
                "password2": "a-long-enough-password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(UserModel.objects.filter(username="ab").exists())

    def test_creation_rejects_a_duplicate_username(self) -> None:
        UserModel.objects.create_user(username="taken")

        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "taken",
                "role": Role.TEACHER,
                "password1": "a-long-enough-password",
                "password2": "a-long-enough-password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserModel.objects.filter(username="taken").count(), 1)

    def test_update_the_role(self) -> None:
        user = UserModel.objects.create_user(username="promote.me")

        self.client.post(
            reverse("admin:accounts_user_change", args=[user.pk]),
            {
                "username": "promote.me",
                "full_name": "",
                "role": Role.ADMINISTRATOR,
                "is_active": "on",
            },
        )

        user.refresh_from_db()
        self.assertEqual(user.role, Role.ADMINISTRATOR)
        self.assertTrue(user.is_staff)

    def test_deactivate_an_account(self) -> None:
        user = UserModel.objects.create_user(username="leaving")

        self.client.post(
            reverse("admin:accounts_user_change", args=[user.pk]),
            {"username": "leaving", "role": Role.TEACHER},
        )

        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_delete_an_account(self) -> None:
        user = UserModel.objects.create_user(username="removable")

        response = self.client.post(
            reverse("admin:accounts_user_delete", args=[user.pk]), {"post": "yes"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserModel.objects.filter(username="removable").exists())

    def test_the_changelist_can_be_searched(self) -> None:
        UserModel.objects.create_user(username="findable.person", full_name="Findable Person")
        UserModel.objects.create_user(username="other.person")

        response = self.client.get(CHANGELIST, {"q": "findable"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["cl"].result_list), 1)

    def test_the_changelist_can_be_filtered_by_role(self) -> None:
        teacher = UserModel.objects.create_user(username="t.one", role=Role.TEACHER)
        administrator = UserModel.objects.create_user(
            username="a.one", role=Role.ADMINISTRATOR
        )

        response = self.client.get(CHANGELIST, {"role__exact": Role.ADMINISTRATOR})

        # The administrator signed in for this request also holds the role, so
        # the filter returns both administrators and excludes the teacher.
        listed = {user.pk for user in response.context["cl"].result_list}
        self.assertIn(administrator.pk, listed)
        self.assertNotIn(teacher.pk, listed)
        self.assertEqual(len(listed), 2)