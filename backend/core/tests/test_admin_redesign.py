"""The administration interface's own markup and styling.

Two things are guarded here, both of which are invisible in a screenshot:

* the admin **renders**. Every page an administrator uses is fetched and checked
  for the template markers, so a template that fails to extend, fails to compile
  or silently falls back to Django's own copy is caught by a test rather than by
  a user.
* the admin **looks like the rest of the product**. The project stylesheet is
  asserted to be loaded, and the same design tokens it defines are asserted to be
  reachable from ``admin.css``, so the two cannot drift apart unnoticed.

And one specific trap, which this file also pins: Django's ``{# ... #}`` comment
tag cannot span lines, so a multi-line ``{# ... #}`` is not a comment at all - it
is emitted into the page as visible text, and any ``{% ... %}`` inside it is
parsed as a real tag. That has broken a template three times in this project, so
it now has a test.
"""

from __future__ import annotations

import re

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.template.loader import get_template
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from accounts.roles import Role
from classrooms.models import Classroom
from core.admin_config import ChallengeAdminConfig
from core.admin_site import ChallengeAdminSite, operational_metrics
from questions.models import Question
from screens.models import InteractiveScreen
from tournaments.models import Tournament

#: The pages an administrator actually uses. Not every changelist is listed: the
#: point is to cover each distinct template - index, changelist, add form, delete
#: confirmation - so a template that stops extending correctly is caught.
ADMIN_PAGES = (
    "/admin/",
    "/admin/accounts/user/",
    "/admin/accounts/user/add/",
    "/admin/classrooms/classroom/",
    "/admin/classrooms/classroom/add/",
    "/admin/screens/interactivescreen/",
    "/admin/screens/interactivescreen/add/",
    "/admin/questions/question/",
    "/admin/competitions/competition/",
    "/admin/tournaments/tournament/",
    "/admin/scoring/answer/",
)


class AdminSiteWiringTests(SimpleTestCase):
    """The project's admin site is the one Django is actually using."""

    def test_the_default_admin_site_is_the_project_one(self) -> None:
        """``admin.site`` is a lazy proxy; the real class is its wrapped object.

        Asserting on ``type(admin.site)`` would pass even when nothing was wired
        up, because that type is Django's ``DefaultAdminSite`` proxy either way.
        """
        self.assertIsInstance(admin.site._wrapped, ChallengeAdminSite)

    def test_the_admin_app_config_points_at_it(self) -> None:
        from django.apps import apps

        config = apps.get_app_config("admin")
        self.assertIsInstance(config, ChallengeAdminConfig)
        self.assertEqual(config.default_site, "core.admin_site.ChallengeAdminSite")

    def test_the_admin_app_label_is_unchanged(self) -> None:
        """``/admin/`` and every ``admin:...`` URL name must survive the swap."""
        from django.apps import apps

        self.assertEqual(apps.get_app_config("admin").name, "django.contrib.admin")

    def test_every_model_is_still_registered(self) -> None:
        self.assertGreater(len(admin.site._registry), 10)

    def test_the_admin_url_names_the_application_uses_all_resolve(self) -> None:
        for name in (
            "admin:index",
            "admin:accounts_user_changelist",
            "admin:classrooms_classroom_changelist",
            "admin:screens_interactivescreen_changelist",
            "admin:questions_question_changelist",
            "admin:competitions_competition_changelist",
            "admin:tournaments_tournament_changelist",
        ):
            with self.subTest(name=name):
                self.assertTrue(reverse(name).startswith("/admin/"))


class AdminTemplateOverrideTests(SimpleTestCase):
    """The project's admin templates win over Django's."""

    def test_the_project_templates_are_the_ones_in_use(self) -> None:
        """Not merely present in the repository - actually resolved.

        ``TEMPLATES["DIRS"]`` is what makes these win, because ``core`` is listed
        after ``django.contrib.admin`` and the app loader walks ``INSTALLED_APPS``
        in order. A test that only checked the files existed would have passed
        while the admin silently rendered Django's own.
        """
        for name in ("admin/base_site.html", "admin/nav_sidebar.html", "admin/index.html"):
            with self.subTest(template=name):
                origin = get_template(name).origin
                self.assertIsNotNone(origin)
                self.assertIn(
                    "/templates/admin/",
                    origin.name,
                    f"{name} resolved to {origin.name}, not the project's copy",
                )

    def test_the_admin_base_still_extends_djangos_own(self) -> None:
        """A UI change must not reimplement the admin; it must extend it.

        ``admin/base.html`` carries the changelist, form, filter and pagination
        machinery in its blocks. Losing that inheritance would quietly delete
        working functionality.
        """
        source = get_template("admin/base_site.html").template.source
        self.assertIn('admin/base.html', source)

    def test_no_admin_template_uses_a_multiline_brace_comment(self) -> None:
        """``{# ... #}`` cannot span lines; a multi-line one leaks into the page.

        Django compiles ``{#.*?#}`` without ``re.DOTALL``, so a multi-line
        ``{# ... #}`` is matched as ordinary text. It then appears in the rendered
        HTML as visible prose, and any ``{% ... %}`` written inside it is parsed
        as a real tag - which is how a comment containing ``{% url %}`` once
        crashed every admin page. ``{% comment %}`` is the correct construct for
        prose that needs more than one line, and this test is what keeps it used.
        """
        offenders = []
        for name in ("admin/base_site.html", "admin/nav_sidebar.html", "admin/index.html"):
            source = get_template(name).template.source
            for match in re.finditer(r"\{#(.*?)#\}", source, re.S):
                if "\n" in match.group(1).strip():
                    offenders.append(name)
        self.assertEqual(
            offenders, [], f"multi-line {{# #}} comments in {offenders}"
        )


class AdminStylingTests(SimpleTestCase):
    """The admin loads the project's own design system."""

    def test_the_stylesheets_are_linked_from_the_admin_base(self) -> None:
        source = get_template("admin/base_site.html").template.source
        self.assertIn("core/css/site.css", source)
        self.assertIn("core/css/admin.css", source)

    def test_admin_css_reuses_the_project_tokens_rather_than_defining_its_own(self) -> None:
        """The two files must share one palette, not two that happen to agree."""
        from django.contrib.staticfiles import finders

        admin_css = finders.find("core/css/admin.css")
        self.assertIsNotNone(admin_css, "core/css/admin.css is not collectable")
        with open(admin_css, encoding="utf-8") as handle:
            admin_source = handle.read()

        site_css = finders.find("core/css/site.css")
        with open(site_css, encoding="utf-8") as handle:
            site_source = handle.read()

        tokens = set(re.findall(r"(--colour-[a-z-]+|--radius|--font-stack):", site_source))
        self.assertTrue(tokens, "no design tokens found in site.css")

        missing = sorted(
            token
            for token in tokens
            if not re.search(rf"var\(\s*{re.escape(token)}\s*[,)]", admin_source)
        )
        self.assertEqual(
            missing,
            [],
            f"admin.css redefines or ignores these design tokens: {missing}",
        )

    def test_admin_css_declares_no_brand_colour_of_its_own(self) -> None:
        """A hex literal in admin.css would be a second palette waiting to drift.

        Only the two ink colours on the accent are allowed as literals, because
        they exist to keep text legible on the green accent and are already used
        that way by ``site.css``'s own buttons.
        """
        from django.contrib.staticfiles import finders

        admin_css = finders.find("core/css/admin.css")
        with open(admin_css, encoding="utf-8") as handle:
            admin_source = handle.read()

        literals = {
            value.lower()
            for value in re.findall(r"#[0-9a-fA-F]{3,8}\b", admin_source)
        }
        # Both already appear in site.css: the ink on the green accent, and the
        # accent's own hover. They are the palette, not a second one.
        allowed = {"#08131c", "#45dda0"}
        self.assertTrue(
            literals <= allowed,
            f"admin.css introduces colours outside the project's palette: "
            f"{sorted(literals - allowed)}",
        )


class AdminOverviewMetricsTests(TestCase):
    """The overview tiles count real rows, and say nothing they cannot support."""

    def setUp(self) -> None:
        self.admin = get_user_model().objects.create_user(
            username="overview.admin", password="a-long-enough-password",
            role=Role.ADMINISTRATOR, full_name="Overview Admin",
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_the_tiles_reflect_the_database(self) -> None:
        Classroom.objects.create(name="Overview Lab")
        Classroom.objects.create(name="Overview Retired", active=False)
        InteractiveScreen.objects.create(name="Overview board")
        Question.objects.create(text="Overview q", options=["a", "b"], correct_option="a")
        Tournament.objects.create(name="Overview championship", created_by=self.admin)

        tiles = {t["label"]: t for t in operational_metrics()["tiles"]}
        self.assertEqual(tiles["Classrooms"]["value"], 2)
        self.assertEqual(tiles["Screens registered"]["value"], 1)
        self.assertEqual(tiles["Question bank"]["value"], 1)
        self.assertEqual(tiles["Teachers"]["value"], 0)
        self.assertEqual(tiles["Tournaments active"]["value"], 0)
        self.assertEqual(tiles["Competitions running"]["value"], 0)

    def test_every_tile_carries_a_label_a_value_and_a_help_line(self) -> None:
        """A number with no unit, or a label with no number, is not a tile."""
        for tile in operational_metrics()["tiles"]:
            with self.subTest(tile=tile["label"]):
                self.assertTrue(str(tile["label"]).strip())
                self.assertIsInstance(tile["value"], int)
                self.assertTrue(str(tile["help"]).strip())
                self.assertIn(
                    tile["tone"], {"neutral", "online", "running"},
                    "an unrecognised tone would fall back to neutral silently",
                )

    def test_a_finished_competition_is_counted_as_completed_not_running(self) -> None:
        from competitions.models import Competition, CompetitionState

        running = Competition.objects.create(title="Running", teacher=self.admin)
        Competition.objects.create(
            title="Done", teacher=self.admin,
            state=CompetitionState.FINISHED,
        )
        tiles = {t["label"]: t for t in operational_metrics()["tiles"]}
        self.assertEqual(tiles["Competitions running"]["value"], 1)
        self.assertEqual(running.pk, running.pk)  # kept referenced; see below
        self.assertEqual(tiles["Competitions running"]["help"], "1 completed")


class AdminRenderingTests(TestCase):
    """Every admin page renders with the project's chrome."""

    def setUp(self) -> None:
        self.admin = get_user_model().objects.create_user(
            username="render.admin", password="a-long-enough-password",
            role=Role.ADMINISTRATOR, full_name="Render Admin",
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_every_page_renders_with_the_sidebar_and_the_stylesheet(self) -> None:
        for url in ADMIN_PAGES:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                body = response.content.decode("utf-8", "replace")
                self.assertIn("admin-sidebar", body, "sidebar missing")
                # Matched loosely: with the production manifest storage the
                # filename carries a content hash.
                self.assertRegex(body, r"core/css/site\.[0-9a-f]+\.css")
                self.assertRegex(body, r"core/css/admin\.[0-9a-f]+\.css")

    def test_the_overview_page_shows_every_tile(self) -> None:
        body = self.client.get("/admin/").content.decode()
        for label in operational_metrics()["tiles"][0].keys():
            self.assertIn("stat__value", body)
        for tile in operational_metrics()["tiles"]:
            with self.subTest(tile=tile["label"]):
                self.assertIn(tile["label"], body)

    def test_no_page_leaks_template_comment_text(self) -> None:
        """Guards the multi-line ``{# #}`` trap at the level it actually shows.

        A comment that fails to be stripped does not raise - it prints. This
        asserts on the rendered HTML, so the failure mode itself is caught.
        """
        for url in ADMIN_PAGES:
            with self.subTest(url=url):
                body = self.client.get(url).content.decode("utf-8", "replace")
                self.assertNotIn("{#", body)
                self.assertNotIn("#}", body)
                self.assertNotIn("{% comment %}", body)

    def test_no_page_carries_debug_detail(self) -> None:
        for url in ADMIN_PAGES:
            with self.subTest(url=url):
                body = self.client.get(url).content.decode("utf-8", "replace")
                self.assertNotIn("Traceback (most recent call last)", body)
                self.assertNotIn("SQLSTATE", body)

    def test_the_pages_have_meaningful_titles(self) -> None:
        """Every page names what it is and the product it belongs to.

        Django's changelist, form and delete templates each build their own
        ``<title>``, so this asserts the two things they all share - a specific
        subject, and the school's name - rather than one literal string that only
        the templates this project overrides would produce.
        """
        for url in ADMIN_PAGES:
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                title = re.search(r"<title>(.*?)</title>", body, re.S)
                self.assertIsNotNone(title, "no <title> on the page")
                rendered = title.group(1).strip()
                self.assertNotEqual(rendered, "")
                self.assertIn("Al Manar Interactive Challenge", rendered)

    def test_the_current_page_is_marked_for_assistive_technology(self) -> None:
        """"You are here" must not be signalled by colour alone."""
        body = self.client.get("/admin/screens/interactivescreen/").content.decode()
        self.assertIn('aria-current="page"', body)
        self.assertIn("admin-sidebar__link--current", body)

    def test_the_sidebar_carries_a_landmark_label(self) -> None:
        body = self.client.get("/admin/").content.decode()
        self.assertIn('aria-label=', body)
        self.assertIn("Administration sections", body)

    def test_the_sidebar_only_links_to_pages_that_exist(self) -> None:
        """Every href in the sidebar must resolve.

        The brief asks for no menu items for functionality that does not exist,
        and the failure mode of a stale link is a 404 an administrator clicks.
        """
        body = self.client.get("/admin/").content.decode()
        sidebar = body.split('class="admin-sidebar"', 1)[1]
        hrefs = set(re.findall(r'href="([^"]+)"', sidebar))
        self.assertTrue(hrefs, "the sidebar rendered no links at all")

        for href in hrefs:
            with self.subTest(href=href):
                if href.startswith("/admin/"):
                    # A model changelist: ask the admin, do not guess the status.
                    self.assertEqual(
                        self.client.get(href).status_code, 200, f"{href} does not resolve"
                    )
                else:
                    self.assertEqual(
                        self.client.get(href, follow=True).status_code, 200,
                        f"{href} does not resolve",
                    )


class AdminAuthorisationIsUnchangedTests(TestCase):
    """The redesign moved nothing about who may reach the admin."""

    def setUp(self) -> None:
        self.teacher = get_user_model().objects.create_user(
            username="redesign.teacher", password="a-long-enough-password",
            role=Role.TEACHER, full_name="Redesign Teacher",
        )
        self.client = Client()

    def test_an_anonymous_visitor_is_refused_every_admin_page(self) -> None:
        for url in ADMIN_PAGES:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])

    def test_a_teacher_is_refused_every_admin_page(self) -> None:
        self.client.force_login(self.teacher)
        for url in ADMIN_PAGES:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])

    def test_the_sidebar_is_not_rendered_to_someone_without_admin_access(self) -> None:
        """The nav cannot become a directory of what exists for an outsider."""
        self.client.force_login(self.teacher)
        body = self.client.get("/admin/screens/interactivescreen/").content.decode()
        self.assertNotIn("admin-sidebar", body)
        self.assertNotIn("admin.css", body)


class ClassroomScreenInterfaceIsUntouchedTests(SimpleTestCase):
    """The classroom screen keeps its own layout.

    The brief is explicit that the desktop admin treatment must not be forced
    onto a board. These assert the screen page does not acquire the admin's
    chrome or stylesheet.
    """

    def test_the_admin_stylesheet_is_not_loaded_by_the_screen_page(self) -> None:
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        screen = InteractiveScreen(name="Untouched board")
        request = RequestFactory().get("/screen/?screen_id=AM-XXXXXX")
        body = render_to_string(
            "screens/status.html", {"screen": screen, "classroom": None}, request=request
        )
        self.assertNotIn("core/css/admin.css", body)
        self.assertNotIn("admin-sidebar", body)

    def test_the_screen_page_still_uses_the_projects_own_stylesheet(self) -> None:
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        screen = InteractiveScreen(name="Untouched board")
        request = RequestFactory().get("/screen/?screen_id=AM-XXXXXX")
        body = render_to_string(
            "screens/status.html", {"screen": screen, "classroom": None}, request=request
        )
        # Hashed by the production manifest storage, so match the pattern.
        self.assertRegex(body, r"core/css/site\.[0-9a-f]+\.css")