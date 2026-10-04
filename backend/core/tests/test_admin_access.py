"""Cross-cutting rules about the administration interface.

These guard the shape of the admin site rather than one model's behaviour: a new
model must not accidentally ship with weaker access control than the rest.
"""

from __future__ import annotations

from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.admin import AdministratorAdminMixin
from core.tests.factories import create_administrator, create_teacher

UserModel = get_user_model()


class AdminRegistrationTests(TestCase):
    def test_every_expected_model_is_registered(self) -> None:
        registered = {model.__name__ for model in admin.site._registry}

        self.assertIn("User", registered)
        self.assertIn("Classroom", registered)
        self.assertIn("InteractiveScreen", registered)

    def test_every_model_admin_requires_the_administrator_role(self) -> None:
        """A new model must not ship with weaker access control."""
        unprotected = [
            model.__name__
            for model in admin.site._registry.values()
            if not isinstance(model, AdministratorAdminMixin)
        ]

        self.assertEqual(
            unprotected,
            [],
            msg=(
                "These ModelAdmins do not enforce the administrator role: "
                + ", ".join(unprotected)
            ),
        )

    def test_the_admin_site_index_lists_all_managed_models(self) -> None:
        self.client.force_login(create_administrator())

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        for label in ("Users", "Classrooms", "Interactive screens"):
            self.assertContains(response, label)

    def test_an_administrator_reaches_every_managed_changelist(self) -> None:
        self.client.force_login(create_administrator())

        for model in admin.site._registry:
            with self.subTest(model=model.__name__):
                url = f"/admin/{model._meta.app_label}/{model._meta.model_name}/"

                self.assertEqual(self.client.get(url).status_code, 200)


class AdminSiteAccessTests(TestCase):
    def setUp(self) -> None:
        self.administrator = create_administrator()
        self.teacher = create_teacher()
        self.inactive_administrator = UserModel.objects.create_user(
            username="retired.admin",
            password="a-long-enough-password",
            role="administrator",
            is_active=False,
        )

    def test_an_active_administrator_has_full_access(self) -> None:
        self.client.force_login(self.administrator)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)

    def test_a_teacher_has_no_access_at_all(self) -> None:
        self.client.force_login(self.teacher)

        for model in admin.site._registry:
            with self.subTest(model=model.__name__):
                url = f"/admin/{model._meta.app_label}/{model._meta.model_name}/"
                response = self.client.get(url)

                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])

    def test_a_teacher_cannot_add_even_with_the_module_listed(self) -> None:
        self.client.force_login(self.teacher)

        response = self.client.get("/admin/classrooms/classroom/add/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_a_deactivated_administrator_loses_access(self) -> None:
        self.client.force_login(self.inactive_administrator)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)


class InstalledApplicationTests(TestCase):
    def test_the_phase_two_applications_are_installed(self) -> None:
        for label in ("accounts", "classrooms", "screens"):
            with self.subTest(app=label):
                self.assertTrue(apps.is_installed(label))

    def test_the_admin_application_is_installed(self) -> None:
        self.assertTrue(apps.is_installed("django.contrib.admin"))