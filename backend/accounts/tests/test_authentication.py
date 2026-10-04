"""Signing in and out as a teacher and as an administrator."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.roles import Role

UserModel = get_user_model()


class AuthenticationTests(TestCase):
    def setUp(self) -> None:
        self.password = "a-sufficiently-long-password"
        self.administrator = UserModel.objects.create_user(
            username="admin",
            password=self.password,
            full_name="Site Administrator",
            role=Role.ADMINISTRATOR,
        )
        self.teacher = UserModel.objects.create_user(
            username="teacher",
            password=self.password,
            full_name="Class Teacher",
            role=Role.TEACHER,
        )

    def test_login_page_is_public(self) -> None:
        response = self.client.get(reverse("accounts:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")

    def test_teacher_can_sign_in(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "teacher", "password": self.password},
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(str(response.wsgi_request.user), "teacher")
        self.assertTrue(response.wsgi_request.user.is_teacher)

    def test_administrator_can_sign_in(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "admin", "password": self.password},
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(response.wsgi_request.user.is_administrator)

    def test_sign_in_honours_the_next_parameter(self) -> None:
        response = self.client.post(
            f"{reverse('accounts:login')}?next={reverse('dashboard')}",
            {"username": "teacher", "password": self.password},
        )

        self.assertRedirects(response, reverse("dashboard"))

    def test_sign_in_with_a_wrong_password_fails(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "teacher", "password": "wrong-password"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_sign_in_with_an_unknown_username_fails(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "nobody", "password": self.password},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_inactive_account_cannot_sign_in(self) -> None:
        self.teacher.is_active = False
        self.teacher.save(update_fields=["is_active"])

        response = self.client.post(
            reverse("accounts:login"),
            {"username": "teacher", "password": self.password},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_signed_in_user_is_redirected_away_from_the_login_page(self) -> None:
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("accounts:login"))

        self.assertEqual(response.status_code, 302)

    def test_sign_out_ends_the_session(self) -> None:
        self.client.force_login(self.teacher)
        self.assertIn("_auth_user_id", self.client.session)

        response = self.client.post(reverse("accounts:logout"))

        self.assertRedirects(response, reverse("core:landing"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_sign_out_requires_a_post(self) -> None:
        """GET sign-out would let any page log a staff member out."""
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("accounts:logout"))

        self.assertEqual(response.status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)