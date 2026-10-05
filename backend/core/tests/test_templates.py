"""Template hygiene, project-wide.

One trap, guarded everywhere rather than where it was last hit.

Django's inline comment tag cannot span lines and cannot contain another inline
comment. Its lexer matches ``{#.*?#}`` without ``re.DOTALL`` and non-greedily, so:

* a comment written over several lines is **not** a comment. It is emitted into
  the page as visible prose, and
* a comment containing the characters of another comment terminates early, leaving
  the rest of the text - including a stray ``#}`` - printed on the page.

Both have shipped here. ``core/base.html`` printed a fragment of its own comment
in front of every anonymous visitor, which included every classroom screen,
because one line of the comment referred to the brace syntax by name. Nothing
failed: the page rendered, the tests passed, and the defect was only visible by
looking at the HTML.

The brace form remains perfectly usable for a single line that contains neither a
newline nor another ``#}``. This module therefore checks the two conditions that
make it unsafe, rather than banning it, so ordinary one-line comments - which the
codebase uses throughout - keep working.

The correct construct for prose of any length is the block comment tag.
"""

from __future__ import annotations

import re
from pathlib import Path

from pathlib import Path

from django.test import SimpleTestCase, TestCase

#: The directory inside the container that holds every app's Python package.
#: Templates live at ``<app>/templates/`` beneath it, and the project's own
#: overrides at ``templates/``. Resolved from this file rather than from settings
#: so it works whether or not the app registry has been populated.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


def all_templates() -> list[Path]:
    """Every template in the project, from every app and the project directory."""
    found: list[Path] = []
    project_override = _BACKEND_DIR / "templates"
    if project_override.is_dir():
        found.extend(sorted(project_override.rglob("*.html")))
    for app_templates in sorted(_BACKEND_DIR.glob("*/templates")):
        if app_templates.is_dir():
            found.extend(sorted(app_templates.rglob("*.html")))
    return found


def inline_comments(source: str) -> list[str]:
    """Every brace-comment body in ``source``, whatever its shape."""
    return [match.group(1) for match in re.finditer(r"\{#(.*?)#\}", source, re.S)]


class TemplateCommentTests(SimpleTestCase):
    def test_the_project_has_templates_to_check(self) -> None:
        """Guards against this module silently passing on an empty file list.

        Asserted by naming templates that must be found, rather than by counting
        files: a count goes stale the moment a template is added or removed, and
        this project's template count legitimately differs between commits.
        """
        found = {path.name for path in all_templates()}
        for name in (
            "base.html",          # core, the root every other template extends
            "dashboard.html",     # core
            "status.html",        # screens
            "screen.html",        # live - the classroom screen
            "login.html",         # accounts
        ):
            with self.subTest(template=name):
                self.assertIn(name, found, f"{name} is not being scanned")

    def test_no_inline_comment_spans_lines(self) -> None:
        offenders = []
        for path in all_templates():
            for body in inline_comments(path.read_text(encoding="utf-8")):
                if "\n" in body.strip():
                    offenders.append(str(path.relative_to(_BACKEND_DIR)))
        self.assertEqual(
            offenders,
            [],
            "These templates use a multi-line {# #} comment, which Django does "
            "not recognise as a comment - it prints it on the page. Use "
            "{% comment %} instead:\n  " + "\n  ".join(sorted(set(offenders))),
        )

    def test_no_inline_comment_contains_another_one(self) -> None:
        """A nested brace comment terminates the outer one early.

        The remainder, including the stray ``#}``, becomes page text. Detected
        by counting: a well-formed document has no more ``#}`` than it has
        opening ``{#``.
        """
        offenders = []
        for path in all_templates():
            source = path.read_text(encoding="utf-8")
            opens = len(re.findall(r"\{#", source))
            closes = len(re.findall(r"#\}", source))
            # Every comment ends with one `#}`; any excess `{#` means one comment
            # opened inside another.
            if opens > closes:
                offenders.append(
                    f"{path.relative_to(_BACKEND_DIR)} "
                    f"({opens} opening vs {closes} closing)"
                )
        self.assertEqual(
            offenders,
            [],
            "These templates have more {# than #}, so at least one inline "
            "comment opens inside another and the leftover text prints on the "
            "page:\n  " + "\n  ".join(sorted(offenders)),
        )


class RenderedOutputTests(TestCase):
    """The same trap, asserted where it actually shows: in the response.

    A comment that fails to strip does not raise. It prints. So these render the
    anonymous pages - the ones every classroom screen loads - and assert the
    markup markers are absent from the bytes the browser receives.
    """

    def test_anonymous_pages_carry_no_template_comment_markers(self) -> None:
        from django.test import Client

        client = Client()
        for url in ("/", "/accounts/login/", "/screen/", "/live/screen/"):
            with self.subTest(url=url):
                response = client.get(url)
                body = response.content.decode("utf-8", "replace")
                self.assertNotIn("{#", body, f"{url} printed an opening comment marker")
                self.assertNotIn("#}", body, f"{url} printed a closing comment marker")
                self.assertNotIn("{% comment %}", body)

    def test_the_csrf_token_form_is_present_on_anonymous_pages(self) -> None:
        """The reason the token exists, asserted directly.

        Without it the screen heartbeat is refused with 403, which is a defect
        this project has already shipped once.
        """
        from django.test import Client

        client = Client()
        for url in ("/", "/accounts/login/", "/screen/", "/live/screen/"):
            with self.subTest(url=url):
                response = client.get(url)
                body = response.content.decode("utf-8", "replace")
                self.assertIn('name="csrfmiddlewaretoken"', body)
                self.assertIn("csrftoken", response.cookies)