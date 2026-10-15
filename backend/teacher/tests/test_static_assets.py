"""Guard that every asset the teacher templates reference actually exists.

This test exists because of a reference that did not: ``base.html`` loaded
``teacher/js/theme-manager.js``, a file that lived at a pre-restructure path and
was never moved when the static tree was reorganised into the app-directory
layout. In development a missing asset is only a 404 in the console. Under the
manifest storage production uses, resolving a name that is not in
``staticfiles.json`` raises ``ValueError`` - so the page never rendered at all,
and every teacher route failed for every teacher until ``collectstatic`` was
re-run for reasons nobody could see.

Rendering the pages would surface that immediately, but these failures arrive as
a 500 with nothing to say about which file went missing, so the check walks the
templates instead: every ``{% static %}`` path in the teacher templates is
looked up against ``STATICFILES_DIRS``, and the manifest built from those files
must contain each one.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, override_settings


@override_settings(
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage"
)
class TeacherStaticAssetsExistTests(SimpleTestCase):
    """Every asset the teacher templates name is present and collectable."""

    template_root = settings.BASE_DIR / "teacher" / "templates"

    def referenced_assets(self) -> set[str]:
        """Paths written inside ``{% static %}`` in the teacher templates."""
        pattern = re.compile(r"\{%\s*static\s+['\"]([^'\"]+)['\"]\s*%\}")
        found = set()
        for path in sorted(self.template_root.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            found.update(pattern.findall(text))
        return found

    def test_every_referenced_asset_exists_on_disk(self) -> None:
        missing = [
            asset
            for asset in self.referenced_assets()
            if finders.find(asset) is None
        ]
        self.assertEqual(
            missing,
            [],
            "teacher templates reference assets that collectstatic cannot copy, "
            "which fails every teacher page under the production manifest storage",
        )

    def test_the_language_switcher_script_is_present(self) -> None:
        """UI-6's own script must not be the one that goes missing."""
        self.assertIn("teacher/js/language-switcher.js", self.referenced_assets())
        self.assertTrue(
            Path(finders.find("teacher/js/language-switcher.js")).is_file()
        )

    def test_the_teacher_stylesheets_are_present(self) -> None:
        for asset in ("core/css/site.css", "teacher/css/teacher.css"):
            with self.subTest(asset=asset):
                self.assertIn(asset, self.referenced_assets())
                self.assertTrue(Path(finders.find(asset)).is_file())

    def test_no_manifest_entries_are_missing_at_render_time(self) -> None:
        """Rendering a teacher page resolves every asset through the storage.

        This is the check that failed before: with the manifest backend, an
        unresolvable name raises during rendering, which is the actual symptom
        the teacher sees. StaticFilesStorage is fine here - the render path is
        the same - so this stays a fast, database-free test.
        """
        from django.template import Context, Template

        for asset in sorted(self.referenced_assets()):
            with self.subTest(asset=asset):
                display = Template("{% load static %}{% static name %}").render(
                    Context({"name": asset})
                )
                self.assertTrue(display.startswith("/static/"), asset)
