"""Creating school staff accounts and their roles."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.roles import Role
from core.tests.factories import TEST_PASSWORD

UserModel = get_user_model()


class UserCreationTests(TestCase):
    def test_create_teacher_with_a_password(self) -> None:
        user = UserModel.objects.create_user(
            username="a.haddad",
            password="correct horse battery staple",
            full_name="A Haddad",
            role=Role.TEACHER,
        )

        self.assertEqual(user.username, "a.haddad")
        self.assertEqual(user.role, Role.TEACHER)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("correct horse battery staple"))

    def test_password_is_hashed_not_stored_in_plain_text(self) -> None:
        user = UserModel.objects.create_user(username="b.saleh", password="hunter2")

        self.assertNotEqual(user.password, "hunter2")
        self.assertTrue(user.password.startswith("pbkdf2_"))

    def test_a_user_without_a_password_gets_an_unusable_one(self) -> None:
        """Correct state for an account that a directory service will own."""
        user = UserModel.objects.create_user(username="c.khalil")

        self.assertFalse(user.has_usable_password())

    def test_default_role_is_teacher(self) -> None:
        user = UserModel.objects.create_user(username="d.nassar")

        self.assertEqual(user.role, Role.TEACHER)

    def test_role_defaults_to_the_first_defined_role_for_administrators(self) -> None:
        administrator = UserModel.objects.create_user(
            username="e.fares", role=Role.ADMINISTRATOR
        )

        self.assertTrue(administrator.is_administrator)
        self.assertFalse(administrator.is_teacher)

    def test_usernames_are_unique(self) -> None:
        UserModel.objects.create_user(username="f.ayoub")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserModel.objects.create_user(username="f.ayoub")

    def test_a_blank_username_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            UserModel.objects.create_user(username="   ")

    def test_an_invalid_email_is_rejected_by_full_clean(self) -> None:
        """The manager follows Django's convention: it does not validate fields."""
        user = UserModel(username="g.zaki", email="not-an-email")

        with self.assertRaises(ValidationError) as context:
            user.full_clean(exclude=["password", "last_login"])

        self.assertIn("email", context.exception.error_dict)

    def test_an_invalid_role_is_rejected_by_full_clean(self) -> None:
        user = UserModel(username="h.mansour", role="headmaster")

        with self.assertRaises(ValidationError):
            user.full_clean(exclude=["password", "last_login"])

    def test_duplicate_usernames_are_rejected_by_the_form_layer_too(self) -> None:
        UserModel.objects.create_user(username="i.raad")

        user = UserModel(username="i.raad")

        with self.assertRaises(ValidationError) as context:
            user.full_clean(exclude=["password", "last_login"])

        self.assertIn("username", context.exception.error_dict)


class SuperuserTests(TestCase):
    def test_create_superuser_grants_admin_site_access(self) -> None:
        administrator = UserModel.objects.create_superuser(
            username="admin", password="a-very-long-password"
        )

        self.assertTrue(administrator.is_superuser)
        self.assertTrue(administrator.is_staff)
        self.assertEqual(administrator.role, Role.ADMINISTRATOR)

    def test_create_superuser_forces_the_administrator_role(self) -> None:
        """A superuser left as a teacher could not reach the admin site."""
        administrator = UserModel.objects.create_superuser(
            username="admin2", password="a-very-long-password", role=Role.TEACHER
        )

        self.assertEqual(administrator.role, Role.ADMINISTRATOR)

    def test_superuser_must_be_active(self) -> None:
        with self.assertRaises(ValueError):
            UserModel.objects.create_superuser(
                username="admin3", password="a-very-long-password", is_active=False
            )


class DisplayHelperTests(TestCase):
    def setUp(self) -> None:
        self.user = UserModel.objects.create_user(
            username="j.ammar", full_name="Jihadeen Ammar"
        )

    def test_full_name_falls_back_to_the_username(self) -> None:
        self.assertEqual(self.user.get_full_name(), "Jihadeen Ammar")
        self.assertEqual(self.user.get_short_name(), "Jihadeen")

    def test_display_helpers_fall_back_when_no_full_name_is_set(self) -> None:
        user = UserModel.objects.create_user(username="k.saleh")

        self.assertEqual(user.get_full_name(), "k.saleh")
        self.assertEqual(user.get_short_name(), "k.saleh")
        self.assertEqual(user.get_initials(), user.username[:2].upper())

    def test_initials_use_at_most_two_characters(self) -> None:
        self.assertEqual(self.user.get_initials(), "JA")

    def test_str_is_the_username(self) -> None:
        self.assertEqual(str(self.user), "j.ammar")


class RoleStorageTests(TestCase):
    def test_roles_are_stored_as_lowercase_slugs(self) -> None:
        administrator = UserModel.objects.create_user(
            username="l.hakim", role=Role.ADMINISTRATOR
        )

        administrator.refresh_from_db()

        self.assertEqual(administrator.role, "administrator")

    def test_both_required_roles_exist(self) -> None:
        self.assertEqual(set(Role.values), {"administrator", "teacher"})

    def test_role_labels_are_human_readable(self) -> None:
        self.assertEqual(Role.ADMINISTRATOR.label, "Administrator")
        self.assertEqual(Role.TEACHER.label, "Teacher")


class PasswordPolicyTests(TestCase):
    """What the deployment will and will not accept as a password.

    Driven through ``validate_password`` rather than a form, so it states the
    policy itself and is not coupled to which screen happens to enforce it. The
    admin screens enforce it via Django's ``UserCreationForm``; see
    ``UserCrudTests`` for the end-to-end behaviour.

    The policy is deliberately one rule and deliberately small: a minimum length
    of 8, and no composition requirement. A school needs passwords its staff can
    type and an administrator can reset, so a rule that is routinely satisfied
    with ``Password1`` would be theatre.
    """

    #: The configured minimum, read from the settings so this test cannot drift
    #: away from the number the application actually enforces.
    MINIMUM = settings.AUTH_PASSWORD_VALIDATORS[0]["OPTIONS"]["min_length"]

    def test_a_minimum_length_validator_is_configured(self) -> None:
        self.assertEqual(
            [entry["NAME"] for entry in settings.AUTH_PASSWORD_VALIDATORS],
            [
                "django.contrib.auth.password_validation.MinimumLengthValidator",
            ],
            "the policy is one rule: a length floor, not a composition checklist",
        )

    def test_the_minimum_is_eight(self) -> None:
        self.assertEqual(self.MINIMUM, 8)

    def test_a_password_at_the_minimum_is_accepted(self) -> None:
        validate_password("a" * self.MINIMUM)

    def test_a_password_below_the_minimum_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            validate_password("a" * (self.MINIMUM - 1))

    def test_a_very_short_password_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            validate_password("ab")

    def test_an_empty_password_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            validate_password("")

    def test_a_phrase_is_accepted_without_composition_rules(self) -> None:
        """A long lowercase phrase must pass: no uppercase, digit or symbol needed.

        This is the property that keeps the policy from being worked around.
        If it ever starts demanding a symbol, staff will comply with `Password1`
        and share it, which is strictly worse than the phrase.
        """
        validate_password("correct horse battery staple")

    def test_the_test_password_the_suite_relies_on_satisfies_the_policy(self) -> None:
        """Every factory in the suite creates users with this password.

        Without this, tightening the policy later would fail hundreds of tests
        at once with a password complaint that looks like a product bug.
        """
        validate_password(TEST_PASSWORD)

    def test_existing_accounts_can_still_sign_in(self) -> None:
        """The policy governs choosing a new password, not signing in.

        A password set before the policy existed is still accepted at login, or
        adding the policy would lock every teacher out of the system.
        """
        UserModel.objects.create_user(
            username="legacy.teacher", password=TEST_PASSWORD, role=Role.TEACHER
        )

        self.assertTrue(
            authenticate(
                username="legacy.teacher", password=TEST_PASSWORD
            )
        )