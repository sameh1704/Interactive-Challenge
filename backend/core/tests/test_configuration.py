"""The project starts successfully and is configured from the environment.

These tests guard the rules that make the application deployable to an unknown
Linux server: no hard-coded secrets, no hard-coded addresses, and static and
media files kept strictly separate.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import django
from django.conf import settings
from django.test import SimpleTestCase

BACKEND_DIR = Path(settings.BASE_DIR)

# Directories that hold installed third-party packages, build output or test
# fixtures rather than shipped application code. The container keeps its virtual
# environment inside the project directory, so these must be pruned explicitly.
#
# Test modules are excluded on purpose: they must contain throwaway passwords and
# dummy addresses to be meaningful. This guard protects what actually ships.
IGNORED_DIRECTORIES = {
    ".venv",
    "venv",
    "site-packages",
    "__pycache__",
    "staticfiles",
    "media",
    "node_modules",
    "tests",
}

SECRET_LIKE_NAMES = {
    "DJANGO_SECRET_KEY",
    "DATABASE_PASSWORD",
    "REDIS_PASSWORD",
}


def _project_python_files() -> list[Path]:
    """Yield every Python file that belongs to this project."""
    for directory, subdirectories, filenames in os.walk(BACKEND_DIR):
        subdirectories[:] = [
            name for name in subdirectories if name not in IGNORED_DIRECTORIES
        ]
        for filename in filenames:
            if filename.endswith(".py"):
                yield Path(directory) / filename

# Anything that looks like an inlined credential or a private network address.
FORBIDDEN_SOURCE_PATTERNS = (
    re.compile(r"""(?i)secret_key\s*=\s*["'][^"'$]{20,}["']"""),
    re.compile(r"""(?i)password\s*=\s*["'][^"'$]+["']"""),
    re.compile(r"(?<![0-9.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![0-9])"),
    re.compile(r"(?<![0-9.])192\.168\.\d{1,3}\.\d{1,3}(?![0-9])"),
    re.compile(r"(?<![0-9.])172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![0-9])"),
)


class DjangoStartsTests(SimpleTestCase):
    def test_django_is_initialised(self) -> None:
        self.assertTrue(django.apps.apps.ready)

    def test_debug_flag_is_a_real_boolean(self) -> None:
        self.assertIsInstance(settings.DEBUG, bool)

    def test_root_urlconf_resolves(self) -> None:
        from django.urls import reverse

        self.assertEqual(reverse("core:landing"), "/")

    def test_core_app_is_installed(self) -> None:
        from django.apps import apps

        self.assertTrue(apps.is_installed("core"))

    def test_whitenoise_serves_static_files(self) -> None:
        self.assertIn(
            "whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE
        )

    def test_security_middleware_is_enabled(self) -> None:
        self.assertIn(
            "django.middleware.security.SecurityMiddleware", settings.MIDDLEWARE
        )


class EnvironmentConfigurationTests(SimpleTestCase):
    def test_secrets_are_not_hard_coded_in_settings(self) -> None:
        """Secrets must come from the process environment, not from the source."""
        if settings.DJANGO_ENV == "production":
            # Production refuses to start without a real key; assert that the
            # configured key is the operator-supplied one.
            self.assertEqual(settings.SECRET_KEY, os.environ["DJANGO_SECRET_KEY"])
        else:
            self.assertTrue(settings.SECRET_KEY)

    def test_no_secret_variable_is_left_at_its_placeholder(self) -> None:
        for name in SECRET_LIKE_NAMES:
            value = os.environ.get(name, "")
            self.assertNotEqual(
                value.strip().lower(),
                "changeme",
                f"{name} still holds its placeholder value.",
            )

    def test_allowed_hosts_is_populated(self) -> None:
        self.assertTrue(settings.ALLOWED_HOSTS)
        self.assertNotIn("*", settings.ALLOWED_HOSTS)

    def test_redis_host_is_configured(self) -> None:
        self.assertEqual(settings.REDIS_HOST, os.environ["REDIS_HOST"])

    def test_database_port_is_numeric(self) -> None:
        self.assertEqual(settings.DATABASE_PORT, int(os.environ["DATABASE_PORT"]))

    def test_no_private_or_literal_address_is_baked_into_the_source(self) -> None:
        """The application must work regardless of the production IP address."""
        offenders: list[str] = []

        for path in _project_python_files():
            content = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN_SOURCE_PATTERNS:
                if pattern.search(content):
                    offenders.append(str(path.relative_to(BACKEND_DIR)))
                    break

        self.assertEqual(
            offenders,
            [],
            msg=(
                "Literal credentials or private network addresses found in: "
                + ", ".join(sorted(set(offenders)))
            ),
        )


class FileStorageConfigurationTests(SimpleTestCase):
    def test_static_and_media_use_separate_roots(self) -> None:
        self.assertNotEqual(settings.STATIC_ROOT, settings.MEDIA_ROOT)

    def test_static_and_media_use_separate_urls(self) -> None:
        self.assertNotEqual(settings.STATIC_URL, settings.MEDIA_URL)
        self.assertFalse(settings.MEDIA_URL.startswith(settings.STATIC_URL))

    def test_static_root_exists_after_collectstatic(self) -> None:
        self.assertTrue(
            settings.STATIC_ROOT.exists(),
            msg=(
                "Static files have not been collected. Run "
                "`python manage.py collectstatic` during the image build."
            ),
        )

    def test_media_root_is_not_inside_static_root(self) -> None:
        self.assertFalse(
            settings.MEDIA_ROOT.is_relative_to(settings.STATIC_ROOT),
            msg="Uploaded media must never be served as a static file.",
        )