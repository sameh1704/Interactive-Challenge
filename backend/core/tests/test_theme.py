"""Focused tests for the light / dark theme switch (UI-7).

The theme is carried in a cookie and rendered onto ``<html data-theme>`` by the
server, which is what makes these tests possible at all: the active theme is a
property of the rendered page, not of a value stored somewhere that has to be
trusted to mean something.

What is checked here:

* the switch writes the cookie and the next request really renders in that theme
* an unsupported or tampered value never becomes the active theme
* the safe default when nothing is stored
* persistence across navigation, logout and login
* the theme and the language preference coexisting, in both orders
* both themes rendering on every teacher-facing page
* the light and dark stylesheets defining the same set of tokens, so a component
  can never be left with a variable that resolves to nothing in one theme
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core import theme as theme_module
from core.tests.utils import create_classroom, create_teacher

SET_THEME_URL = "/theme/"
THEME_COOKIE = settings.THEME_COOKIE_NAME

TEACHER_PAGES = (
    "teacher:dashboard",
    "teacher:competitions",
    "teacher:create_competition",
    "teacher:questions",
    "teacher:create_question",
    "teacher:tournaments",
    "teacher:create_tournament",
    "teacher:reports",
    "teacher:hall_of_fame",
    "teacher:help",
)


class ThemeCookieTests(TestCase):
    """The cookie is what the server reads, so it is the storage policy."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="ui7.theme.teacher")
        self.client.force_login(self.teacher)

    def switch(self, code: str, next_url: str = "/teacher/"):
        return self.client.post(SET_THEME_URL, data={"theme": code, "next": next_url})

    def html_tag(self, content: bytes) -> str:
        return re.search(rb"<html[^>]*>", content).group().decode()

    # ---------------------------------------------------------- the switch

    def test_switching_to_dark_sets_the_cookie_and_renders_dark(self) -> None:
        response = self.switch("dark")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies[THEME_COOKIE].value, "dark")

        follow_up = self.client.get(reverse("teacher:dashboard"))
        self.assertIn('data-theme="dark"', self.html_tag(follow_up.content))
        # The cookie is the persistence mechanism, so it must outlive the browser.
        self.assertTrue(int(response.cookies[THEME_COOKIE]["max-age"]) > 0)

    def test_switching_to_light_sets_the_cookie_and_renders_light(self) -> None:
        self.switch("dark")
        self.assertEqual(self.switch("light").cookies[THEME_COOKIE].value, "light")

        follow_up = self.client.get(reverse("teacher:dashboard"))
        self.assertIn('data-theme="light"', self.html_tag(follow_up.content))

    def test_unsupported_theme_is_rejected_and_changes_nothing(self) -> None:
        response = self.switch("purple")
        self.assertEqual(response.status_code, 302)
        # Nothing is written: an unknown theme must not be able to put a value in
        # the cookie that the server would then have to guess what to do with.
        self.assertNotIn(THEME_COOKIE, response.cookies)

        follow_up = self.client.get(reverse("teacher:dashboard"))
        self.assertIn('data-theme="light"', self.html_tag(follow_up.content))

    def test_tampered_cookie_falls_back_to_the_default(self) -> None:
        """A stored value that is not a theme is ignored, not honoured."""
        for value in ("purple", "dark;", "almanar_theme=dark", "DARK", ""):
            with self.subTest(value=value):
                self.client.cookies[THEME_COOKIE] = value
                response = self.client.get(reverse("teacher:dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertIn('data-theme="light"', self.html_tag(response.content))

    def test_get_request_changes_nothing(self) -> None:
        """A theme is a preference, not something a link can flip."""
        response = self.client.get(SET_THEME_URL, data={"theme": "dark", "next": "/teacher/"})
        self.assertNotIn(THEME_COOKIE, response.cookies)
        self.assertIn(
            'data-theme="light"',
            self.html_tag(self.client.get(reverse("teacher:dashboard")).content),
        )

    def test_switch_is_not_an_open_redirect(self) -> None:
        response = self.switch("dark", next_url="https://evil.example.com/steal")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example.com", response.headers["location"])

    # ------------------------------------------------------- safe defaults

    def test_default_theme_when_no_preference_is_stored(self) -> None:
        response = self.client.get(reverse("teacher:dashboard"))
        self.assertIn('data-theme="light"', self.html_tag(response.content))

    def test_theme_module_resolves_unknown_values_to_the_default(self) -> None:
        self.assertEqual(theme_module.resolve("dark").name, "dark")
        self.assertEqual(theme_module.resolve("light").name, "light")
        self.assertEqual(theme_module.resolve(None).name, theme_module.DEFAULT_THEME)
        self.assertEqual(theme_module.resolve("purple").name, theme_module.DEFAULT_THEME)
        self.assertTrue(theme_module.resolve("purple").is_light)

    def test_cookie_is_not_a_session_cookie(self) -> None:
        """A session cookie would die with the browser, which is not persistence."""
        self.assertTrue(settings.THEME_COOKIE_AGE > 0)
        self.assertFalse(settings.THEME_COOKIE_HTTPONLY, "the switch writes it in JS")

    # ---------------------------------------------------------- persistence

    def test_theme_persists_across_pages_and_navigation(self) -> None:
        self.switch("dark")
        for route in TEACHER_PAGES:
            with self.subTest(route=route):
                content = self.client.get(reverse(route)).content
                self.assertIn('data-theme="dark"', self.html_tag(content))

    def test_theme_persists_across_logout_and_login(self) -> None:
        """The theme cookie is its own cookie, so logout flushing the session keeps it.

        Modelled the way the browser does it rather than with ``client.logout()``:
        that helper replaces the whole cookie jar with an empty one, which no
        browser does - a real sign-out flushes the *session* and leaves every
        other cookie on the device alone. The two claims made here are therefore
        about the server: signing out does not clear the theme, and a request that
        still carries it renders in it.
        """
        self.switch("dark")
        stored = self.client.cookies[THEME_COOKIE].value
        self.assertEqual(stored, "dark")

        signed_out = self.client.post(reverse("accounts:logout"))
        # Signing out says nothing about the theme cookie either way...
        self.assertNotIn(THEME_COOKIE, signed_out.cookies, "logout must not clear the theme")

        # ...so a request that still carries it is still dark, and signing back in
        # restores the session without touching the theme.
        self.client.force_login(self.teacher)
        self.assertIn(
            'data-theme="dark"',
            self.html_tag(self.client.get(reverse("teacher:dashboard")).content),
        )

    def test_theme_is_per_browser_not_per_account(self) -> None:
        """One teacher's theme must not follow another teacher's account.

        A second browser with no cookie gets the default even though the same
        account is stored on the server with the first browser's preference.
        """
        self.switch("dark")
        other = self.client_class()
        other.force_login(self.teacher)
        self.assertIn(
            'data-theme="light"',
            self.html_tag(other.get(reverse("teacher:dashboard")).content),
        )

    # ------------------------------------------------------- coexistence

    def test_theme_and_language_are_independent_cookies(self) -> None:
        """Switching the theme must not touch the language, or the direction."""
        self.client.post("/teacher/set_language/", data={"language": "ar", "next": "/teacher/"})
        arabic_tag = self.html_tag(self.client.get(reverse("teacher:dashboard")).content)
        self.assertIn('lang="ar"', arabic_tag)
        self.assertIn('dir="rtl"', arabic_tag)

        self.switch("dark")
        after_switch = self.html_tag(self.client.get(reverse("teacher:dashboard")).content)
        # Same language, same direction, new theme.
        self.assertIn('lang="ar"', after_switch)
        self.assertIn('dir="rtl"', after_switch)
        self.assertIn('data-theme="dark"', after_switch)
        self.assertEqual(self.client.cookies["django_language"].value, "ar")

    def test_switching_language_does_not_change_the_theme(self) -> None:
        self.switch("dark")
        self.client.post("/teacher/set_language/", data={"language": "en", "next": "/teacher/"})
        tag = self.html_tag(self.client.get(reverse("teacher:dashboard")).content)
        self.assertIn('data-theme="dark"', tag)
        self.assertIn('dir="ltr"', tag)

    # ---------------------------------------------------------- rendering

    def test_both_themes_render_every_teacher_page(self) -> None:
        for code in ("light", "dark"):
            self.switch(code)
            for route in TEACHER_PAGES:
                with self.subTest(theme=code, route=route):
                    response = self.client.get(reverse(route))
                    self.assertEqual(response.status_code, 200, route)
                    self.assertIn(
                        f'data-theme="{code}"', self.html_tag(response.content)
                    )

    def test_shared_pages_render_in_both_themes(self) -> None:
        for code in ("light", "dark"):
            self.switch(code)
            for route in ("core:landing", "dashboard", "reports:index", "tournaments:list"):
                with self.subTest(theme=code, route=route):
                    response = self.client.get(reverse(route))
                    self.assertEqual(response.status_code, 200, route)
                    self.assertIn(f'data-theme="{code}"', self.html_tag(response.content))

    def test_the_live_dashboard_renders_in_both_themes(self) -> None:
        from core.tests.utils import create_competition

        competition = create_competition(teacher=self.teacher, title="جولة الاختبار")
        for code in ("light", "dark"):
            self.switch(code)
            response = self.client.get(
                reverse("live:teacher_dashboard", args=[competition.pk])
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(f'data-theme="{code}"', self.html_tag(response.content))

    def test_wizard_data_survives_a_theme_switch(self) -> None:
        """The switch does not reload the page, so nothing in a form is lost."""
        from teacher.tournament_wizard import SESSION_KEY

        create_classroom(name="قاعة الاختبار")
        self.client.post(
            reverse("teacher:create_tournament"),
            data={
                "action": "next",
                "name": "بطولة الاختبار",
                "subject": "أحياء",
                "grade": "8",
                "season": "2026",
            },
        )
        self.assertEqual(self.client.session[SESSION_KEY]["step"], "participants")

        self.switch("dark", next_url="/teacher/tournaments/create/")
        state = self.client.session[SESSION_KEY]
        self.assertEqual(state["tournament"]["name"], "بطولة الاختبار")
        self.assertEqual(state["step"], "participants")
        self.assertIn(
            'data-theme="dark"',
            self.html_tag(self.client.get(reverse("teacher:create_tournament")).content),
        )

    # ------------------------------------------------------------ the switch

    def test_the_switch_is_on_every_teacher_page(self) -> None:
        for route in TEACHER_PAGES:
            with self.subTest(route=route):
                content = self.client.get(reverse(route)).content.decode()
                self.assertIn("data-theme-form", content)
                self.assertIn('name="theme" value="light"', content)
                self.assertIn('name="theme" value="dark"', content)

    def test_the_switch_marks_the_active_theme(self) -> None:
        light = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertIn('value="light"', light)
        self.assertIn('aria-pressed="true"', light)
        self.assertIn('aria-pressed="false"', light)

        self.switch("dark")
        dark = self.client.get(reverse("teacher:dashboard")).content.decode()
        # Exactly one control is pressed, and it is the dark one.
        pressed = re.findall(r'name="theme" value="(\w+)"[^>]*aria-pressed="(\w+)"', dark)
        self.assertEqual(pressed, [("light", "false"), ("dark", "true")])

    def test_the_switch_is_labelled_and_grouped_for_assistive_technology(self) -> None:
        content = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertIn('role="group"', content)
        self.assertIn('aria-label="Theme"', content)
        # Both controls are real submit buttons, so the keyboard reaches and
        # fires them without any JavaScript.
        self.assertIn('type="submit"', content)

    def test_the_switch_label_follows_the_language(self) -> None:
        """The switcher is translated exactly like the rest of the workspace."""
        english = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertIn('aria-label="Theme"', english)
        self.assertRegex(english, r'value="light"[^>]*>\s*Light\s*<')
        self.assertRegex(english, r'value="dark"[^>]*>\s*Dark\s*<')

        self.client.post("/teacher/set_language/", data={"language": "ar", "next": "/teacher/"})
        arabic = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertIn('aria-label="المظهر"', arabic)
        self.assertRegex(arabic, r'value="light"[^>]*>\s*فاتح\s*<')
        self.assertRegex(arabic, r'value="dark"[^>]*>\s*داكن\s*<')
        # The language switcher beside it keeps its own bilingual labels.
        self.assertIn("العربية", arabic)


class ThemeStaticAssetTests(SimpleTestCase):
    """The stylesheet and the script must exist, and say the same things.

    A missing asset is a 404 in development and, under the manifest storage
    production uses, a missing manifest entry that fails the whole page - which is
    how the earlier theme reference came to break every teacher route. A missing
    *token* is quieter still: `var(--colour-accent)` fails silently, so a
    component just stops being coloured.
    """

    def test_the_theme_script_and_stylesheet_are_referenced_and_present(self) -> None:
        for asset in ("core/js/theme-manager.js", "core/css/site.css"):
            with self.subTest(asset=asset):
                self.assertIsNotNone(finders.find(asset), asset)
                self.assertTrue(Path(finders.find(asset)).is_file(), asset)

    def test_no_template_references_a_missing_asset(self) -> None:
        """Every static name in a template resolves to a real file."""
        pattern = re.compile(r"\{%\s*static\s+['\"]([^'\"]+)['\"]\s*%\}")
        checked = 0
        for directory in ("core/templates", "teacher/templates", "live/templates"):
            root = settings.BASE_DIR / directory
            for path in sorted(root.rglob("*.html")):
                text = path.read_text(encoding="utf-8")
                for asset in pattern.findall(text):
                    checked += 1
                    with self.subTest(template=str(path.name), asset=asset):
                        self.assertIsNotNone(finders.find(asset), f"{path.name}: {asset}")
        self.assertGreater(checked, 10, "the walk should have found real references")

    def test_dark_theme_defines_every_colour_the_light_theme_defines(self) -> None:
        """Both themes must define the same set of colour tokens.

        A token defined in `:root` but missing from `[data-theme="dark"]` does not
        error: the variable simply stops resolving, and the property that used it
        falls back to whatever inherits. That is how a component quietly goes
        unthemed in one of the two themes, so the parity is asserted here rather
        than left to be noticed on screen.

        Only `--colour-*` are compared. `--radius` and `--font-stack` are the same
        in both themes by design, and a rule demanding they be duplicated would be
        noise.
        """
        css = Path(finders.find("core/css/site.css")).read_text(encoding="utf-8")

        root_block = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL)
        dark_block = re.search(r'\[data-theme="dark"\]\s*\{(.*?)\}', css, re.DOTALL)
        self.assertIsNotNone(root_block, "the light theme must be defined on :root")
        self.assertIsNotNone(dark_block, "the dark theme must be defined")

        def colour_names(block: str) -> set[str]:
            return {
                name
                for name in re.findall(r"--([\w-]+)\s*:", block)
                if name.startswith("colour-")
            }

        light = colour_names(root_block.group(1))
        dark = colour_names(dark_block.group(1))
        self.assertTrue(light, "no colour tokens found in the light theme")
        self.assertEqual(
            light - dark,
            set(),
            "colour tokens defined in the light theme but missing from the dark one",
        )

    def test_both_themes_declare_a_colour_scheme(self) -> None:
        """Without it the browser keeps its own light form controls and scrollbars."""
        css = Path(finders.find("core/css/site.css")).read_text(encoding="utf-8")
        self.assertIn("color-scheme: light", css)
        self.assertIn("color-scheme: dark", css)

    def test_no_hard_coded_colour_is_left_in_the_themed_sheets(self) -> None:
        """A literal colour in a themed sheet is a colour that does not invert.

        `#fff`, `white` and a handful of near-black inks are the ones that survive
        a styling pass by accident; they are checked for by name, because a
        contrast checker cannot see what a stylesheet means to do.

        The token blocks are skipped first: that is exactly where the two palettes
        are defined, and `--colour-surface: #ffffff` is a token working, not a
        colour that failed to invert. What is checked is the rest of the sheet -
        every rule that paints something.
        """
        for asset in (
            "core/css/site.css",
            "teacher/css/teacher.css",
            "live/css/teacher.css",
            "tournaments/css/tournaments.css",
        ):
            css = Path(finders.find(asset)).read_text(encoding="utf-8")
            with self.subTest(asset=asset):
                # Drop comments (they name colours as prose) and the token blocks
                # that legitimately define the two palettes.
                body = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
                body = re.sub(r"^\s*--[\w-]+\s*:[^;]*;", "", body, flags=re.MULTILINE)
                self.assertNotRegex(body, r"#(fff|ffffff|000|000000)\b")
                self.assertNotRegex(body, r"(?<![\w-])white(?![\w-])")

    def test_the_theme_script_validates_what_it_reads_and_writes(self) -> None:
        """The allowlist is what keeps a tampered cookie off the screen."""
        script = Path(finders.find("core/js/theme-manager.js")).read_text(encoding="utf-8")
        self.assertIn("THEMES", script)
        self.assertIn("decodeURIComponent", script)
        # The cookie is written with a max-age, or the preference is a session
        # cookie and does not survive a restart.
        self.assertIn("max-age=", script)

    def test_the_head_script_validates_before_applying(self) -> None:
        """The no-flash script must not apply an unrecognised value either."""
        from django.template.loader import get_template

        rendered = get_template("core/partials/theme_init.html").render({"theme_cookie_name": THEME_COOKIE})
        self.assertIn("ALLOWED", rendered)
        self.assertIn("data-theme", rendered)
        self.assertIn(THEME_COOKIE, rendered)


class ThemeResponsiveTests(SimpleTestCase):
    """The layout must not overflow at the widths teachers actually use.

    Measured from the stylesheet rather than in a browser: the deployment image
    carries no browser, so what is checked here is what the CSS *states* about
    its own widths, which is where a horizontal overflow is decided.

    ``.page`` is ``width: min(64rem, 100% - 2.5rem)``, so the narrowest content
    column the interface ever gets is 320px - 40px = **280px / 17.5rem**.
    """

    #: The narrowest viewport in the supported set.
    NARROWEST = 320
    CONTENT_REM = 17.5

    def stylesheets(self) -> list[tuple[str, str]]:
        sheets = []
        for asset in (
            "core/css/site.css",
            "teacher/css/teacher.css",
            "live/css/teacher.css",
            "tournaments/css/tournaments.css",
        ):
            path = finders.find(asset)
            self.assertIsNotNone(path, asset)
            sheets.append((asset, Path(path).read_text(encoding="utf-8")))
        return sheets

    @staticmethod
    def _without_media(css: str) -> str:
        """The stylesheet with its media-query bodies removed.

        A width declared inside a ``@media (max-width: ...)`` rule has already
        been collapsed by that query, so it says nothing about the narrowest
        viewport and must not be counted.
        """
        return re.sub(
            r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css, flags=re.DOTALL
        )

    def test_no_grid_track_is_wider_than_the_narrowest_content_column(self) -> None:
        """A grid track whose minimum exceeds 280px overflows its container.

        ``minmax(0, ...)`` is fine: a zero minimum collapses, which is how the
        flexible column of the question builder shrinks away.

        The one exception is the question builder's preview column, and it is
        proven safe by the test below rather than by being ignored here.
        """
        offenders = []
        for asset, css in self.stylesheets():
            for value in re.findall(r"minmax\(\s*([0-9.]+)rem", self._without_media(css)):
                if float(value) > self.CONTENT_REM:
                    offenders.append(f"{asset}: minmax({value}rem, ...)")
        allowed = {"teacher/css/teacher.css: minmax(18rem, ...)"}
        self.assertEqual(
            [offender for offender in offenders if offender not in allowed],
            [],
            "grid tracks that overflow a 320px viewport with no media query to save them",
        )

    def test_the_two_column_grid_collapses_before_the_narrowest_viewport(self) -> None:
        """The question builder's 18rem preview column is guarded by a media query.

        It is the one rule in the sheets whose minimum (18rem = 288px) is wider
        than the 280px the content column gets at 320px. That is safe only
        because a media query collapses the layout to one column first; this
        asserts the guard is still there rather than trusting nobody deletes it.
        """
        css = dict(self.stylesheets())["teacher/css/teacher.css"]
        self.assertIn("minmax(18rem, 23rem)", css)
        media = re.findall(
            r"@media\s*\(max-width:\s*\d+px\)\s*\{(.*?)\n\}", css, re.DOTALL
        )
        guard_found = any(
            "question-builder__layout" in block
            and "grid-template-columns: minmax(0, 1fr);" in block
            for block in media
        )
        self.assertTrue(guard_found, "the question-builder layout never collapses")

    def test_tables_scroll_inside_their_region(self) -> None:
        """A wide table must scroll, not push the page sideways.

        The alternative is a horizontal scrollbar on the whole document, which on
        a phone takes the navigation with it.
        """
        css = dict(self.stylesheets())["teacher/css/teacher.css"]
        self.assertIn("overflow-x: auto", css)
        self.assertIn("overscroll-behavior-inline: contain", css)
        # Every table the workspace renders is inside a scrolling region. Reports
        # is a list of links, not a table, so it is not in this set.
        for name in ("questions.html", "competitions.html", "tournaments.html"):
            source = (
                settings.BASE_DIR / "teacher" / "templates" / "teacher" / name
            ).read_text(encoding="utf-8")
            with self.subTest(template=name):
                self.assertIn('class="table-scroll"', source)

    def test_the_masthead_wraps_at_every_level(self) -> None:
        """The masthead carries ten nav links plus the user, two switchers and
        sign-out; without wrapping, it would push the page sideways on a phone."""
        css = dict(self.stylesheets())["core/css/site.css"]
        nav = re.search(r"\.masthead__nav\s*\{(.*?)\}", css, re.DOTALL).group(1)
        self.assertIn("flex-wrap: wrap", nav)
        self.assertIn("max-width: 100%", nav)
        self.assertIn("min-width: 0", nav)
        # The small-screen rule tightens the gaps rather than reflowing.
        self.assertIn("@media (max-width: 32rem)", css)

    def test_nothing_is_wider_than_a_phone_in_the_themed_sheets(self) -> None:
        """No fixed pixel width above 320px, anywhere but a media query."""
        offenders = []
        for asset, css in self.stylesheets():
            for value in re.findall(
                r"(?:min-)?width:\s*(\d{3,})px", self._without_media(css)
            ):
                if int(value) > self.NARROWEST:
                    offenders.append(f"{asset}: width {value}px")
        self.assertEqual(offenders, [], "fixed widths wider than a 320px viewport")

    def test_status_is_never_carried_by_colour_alone(self) -> None:
        """Every status badge must still say what it is.

        The badges are a coloured chip with the status word inside it; removing
        the word would leave a teacher matching colours, which fails anyone who
        cannot separate them - and this is an interface used in classrooms.
        """
        for name in ("questions.html", "competitions.html", "tournaments.html"):
            source = (
                settings.BASE_DIR / "teacher" / "templates" / "teacher" / name
            ).read_text(encoding="utf-8")
            with self.subTest(template=name):
                for badge in re.findall(
                    r'<span class="badge[^"]*"[^>]*>(.*?)</span>', source, re.DOTALL
                ):
                    self.assertTrue(
                        re.search(r"[\w\u0600-\u06FF]", badge),
                        f"{name}: a badge carries no status text: {badge.strip()!r}",
                    )
