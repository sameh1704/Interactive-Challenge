"""Role-based permission rules."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.permissions import (
    RoleRequiredMixin,
    is_administrator,
    is_authenticated_staff,
    is_staff_member,
    is_teacher,
)
from accounts.roles import Role

UserModel = get_user_model()


class PermissionHelperTests(TestCase):
    def setUp(self) -> None:
        self.administrator = UserModel.objects.create_user(
            username="admin", password="pw-admin-long", role=Role.ADMINISTRATOR
        )
        self.teacher = UserModel.objects.create_user(
            username="teacher", password="pw-teacher-long", role=Role.TEACHER
        )
        self.inactive = UserModel.objects.create_user(
            username="inactive", password="pw-inactive-long", is_active=False
        )

    def test_anonymous_users_are_never_permitted(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        anonymous = AnonymousUser()

        self.assertFalse(is_authenticated_staff(anonymous))
        self.assertFalse(is_administrator(anonymous))
        self.assertFalse(is_teacher(anonymous))
        self.assertFalse(is_staff_member(anonymous))

    def test_none_is_never_permitted(self) -> None:
        self.assertFalse(is_authenticated_staff(None))
        self.assertFalse(is_administrator(None))

    def test_administrators_are_recognised(self) -> None:
        self.assertTrue(is_administrator(self.administrator))
        self.assertTrue(is_staff_member(self.administrator))
        self.assertFalse(is_teacher(self.administrator))

    def test_teachers_are_recognised(self) -> None:
        self.assertTrue(is_teacher(self.teacher))
        self.assertTrue(is_staff_member(self.teacher))
        self.assertFalse(is_administrator(self.teacher))

    def test_inactive_accounts_are_never_permitted(self) -> None:
        inactive_teacher = UserModel.objects.create_user(
            username="retired-teacher", password="pw-retired-long", is_active=False
        )
        inactive_administrator = UserModel.objects.create_user(
            username="retired-admin",
            password="pw-retired-long",
            role=Role.ADMINISTRATOR,
            is_active=False,
        )

        self.assertFalse(is_staff_member(inactive_teacher))
        self.assertFalse(is_teacher(inactive_teacher))
        self.assertFalse(is_administrator(inactive_administrator))
        self.assertFalse(is_authenticated_staff(self.inactive))

    def test_is_staff_follows_the_role(self) -> None:
        self.assertTrue(self.administrator.is_staff)
        self.assertFalse(self.teacher.is_staff)
        self.assertFalse(self.inactive.is_staff)


class AdminSiteAccessTests(TestCase):
    """The Django admin site is the administrator's management interface."""

    def setUp(self) -> None:
        self.administrator = UserModel.objects.create_user(
            username="admin", password="pw-admin-long", role=Role.ADMINISTRATOR
        )
        self.teacher = UserModel.objects.create_user(
            username="teacher", password="pw-teacher-long", role=Role.TEACHER
        )

    def test_administrator_can_reach_the_admin_site(self) -> None:
        self.client.force_login(self.administrator)

        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_teacher_is_refused_the_admin_site(self) -> None:
        self.client.force_login(self.teacher)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_anonymous_visitor_is_sent_to_the_admin_login(self) -> None:
        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])


class RoleRequiredMixinTests(TestCase):
    """The mixin is used by views; test it through a throwaway view."""

    def setUp(self) -> None:
        from django.http import HttpResponse
        from django.test import RequestFactory
        from django.views.generic import View

        class ProbeView(RoleRequiredMixin, View):
            allowed_roles = (Role.ADMINISTRATOR,)

            def get(self, request, *args, **kwargs):
                return HttpResponse("ok")

        self.factory = RequestFactory()
        self.view = ProbeView.as_view()

    def _get(self, user):
        request = self.factory.get("/probe/")
        request.user = user
        return self.view(request)

    def test_anonymous_visitor_is_redirected_to_sign_in(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        response = self._get(AnonymousUser())

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_administrator_is_allowed(self) -> None:
        administrator = UserModel.objects.create_user(
            username="admin", password="pw-admin-long", role=Role.ADMINISTRATOR
        )

        self.assertEqual(self._get(administrator).status_code, 200)

    def test_teacher_is_refused(self) -> None:
        from django.core.exceptions import PermissionDenied

        teacher = UserModel.objects.create_user(username="teacher", password="pw-long")

        with self.assertRaises(PermissionDenied):
            self._get(teacher)

    def test_an_account_with_no_role_is_refused(self) -> None:
        from django.core.exceptions import PermissionDenied

        # A stub, because a real account cannot hold an empty role: the database
        # constraint forbids it. This proves the authorisation layer still
        # refuses anything that is not a known role.
        class Roleless:
            is_authenticated = True
            is_active = True
            role = ""

        with self.assertRaises(PermissionDenied):
            self._get(Roleless())