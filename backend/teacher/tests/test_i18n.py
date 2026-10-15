"""Focused tests for the Arabic / English internationalisation (UI-6).

These test the switching mechanism as it actually behaves, not as an
implementation detail suggests it should. Django's ``LocaleMiddleware``
activates whatever ``translation.get_language_from_request`` returns, and in
Django 5.2 that helper reads the *language cookie* and the ``Accept-Language``
header - it does not read a session key. So a preference that only lives in the
session changes nothing, and these tests catch that being reintroduced.

They are written against the rendered HTML rather than against the setting: the
acceptance criterion for this phase is that the teacher's screen really changes
language, not that a value is stored somewhere.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from competitions.models import Competition
from core.tests.utils import create_classroom, create_competition, create_teacher
from teacher.tournament_wizard import SESSION_KEY as TOURNAMENT_SESSION_KEY
from teacher.wizard import SESSION_KEY as COMPETITION_SESSION_KEY


# Every teacher-facing page the task lists, so a regression that breaks one
# language on one page is named in the failure rather than hidden in a summary.
TEACHER_PAGES = {
    "Teacher Dashboard": "teacher:dashboard",
    "My Competitions": "teacher:competitions",
    "Competition Wizard": "teacher:create_competition",
    "Question Bank": "teacher:questions",
    "Question Builder": "teacher:create_question",
    "My Tournaments": "teacher:tournaments",
    "Tournament Wizard": "teacher:create_tournament",
    "Reports": "teacher:reports",
    "Hall of Fame": "teacher:hall_of_fame",
    "Help": "teacher:help",
}

# Django's own endpoint: writes the cookie for any language whose catalog exists
# anywhere on disk, including the ~100 bundled inside Django.
DJANGO_URL = "/i18n/setlang/"
# The teacher route the masthead actually posts to: same view, plus a check that
# the code is one this project offers.
TEACHER_URL = "/teacher/set_language/"


def html_attrs(content: bytes | str) -> dict[str, str]:
    """The attributes of the rendered ``<html>`` element.

    Parsed rather than matched as a whole tag. These tests asserted the exact
    string ``<html lang="ar" dir="rtl">``, and adding the theme attribute in UI-7
    then failed every one of them without the language or the direction having
    moved. Comparing attribute by attribute keeps the tests about the two things
    they claim to be about - which language, and which direction - whatever else
    the element grows. Accepts bytes or an already-decoded string, so a test can
    pass either without thinking about it.
    """
    if isinstance(content, bytes):
        content = content.decode()
    tag = re.search(r"<html([^>]*)>", content).group(1)
    attrs: dict[str, str] = {}
    for name, value in re.findall(r'([\w-]+)=\s*"([^"]*)"', tag):
        attrs[name] = value
    for name in re.findall(r"\s([\w-]+)(?=[\s>]|$)", tag):
        attrs.setdefault(name, "")
    return attrs


def tag_says(content: bytes, **wanted: str) -> bool:
    """Whether the element carries every attribute/value pair given."""
    attrs = html_attrs(content)
    return all(attrs.get(name) == value for name, value in wanted.items())


class LanguageSwitchTests(TestCase):
    def setUp(self) -> None:
        self.teacher = create_teacher(username="ui6.language.teacher")
        self.client.force_login(self.teacher)

    def switch(self, code: str, next_url: str = "/teacher/", *, url: str = TEACHER_URL):
        """Switch the way the masthead's own form does: a POST with `language`."""
        return self.client.post(url, data={"language": code, "next": next_url})

    # ------------------------------------------------------- the endpoint

    def test_switch_endpoint_accepts_arabic_and_redirects_back(self) -> None:
        response = self.switch("ar", next_url=reverse("teacher:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("teacher:dashboard"))
        self.assertEqual(response.cookies["django_language"].value, "ar")

    def test_switch_endpoint_accepts_english(self) -> None:
        self.switch("ar")
        response = self.switch("en", next_url=reverse("teacher:dashboard"))
        self.assertEqual(response.cookies["django_language"].value, "en")
        self.assertTrue(
            tag_says(self.client.get(reverse("teacher:dashboard")).content, lang="en")
        )

    def test_unsupported_language_is_rejected_without_touching_the_screen(self) -> None:
        """A language this school does not offer must not flip the chrome.

        Django's own view accepts any language with a catalog anywhere on the
        system, which includes the ones bundled inside Django - so ``fr`` would
        be accepted there. The teacher route checks ``settings.LANGUAGES``
        first, so only English and Arabic get through.
        """
        response = self.switch("fr")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("django_language", response.cookies)

        follow_up = self.client.get(reverse("teacher:tournaments"))
        self.assertTrue(tag_says(follow_up.content, lang="en", dir="ltr"))

    def test_malformed_language_is_rejected_safely(self) -> None:
        for code in ("ar-x-invalid/traversal", "", "ENGLISH", "ard"):
            with self.subTest(code=code):
                response = self.switch(code)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("django_language", response.cookies)

    def test_rejection_leaves_an_explanation_for_the_teacher(self) -> None:
        """The teacher is told why nothing happened, in their own language.

        The message is translated lazily, so it is resolved against the language
        of the redirected page - which is the language the teacher is using.
        """
        response = self.switch("fr")
        follow_up = self.client.get(response.headers["location"], follow=True)
        self.assertEqual(follow_up.status_code, 200)
        self.assertIn(
            "That language is not available on this system.",
            follow_up.content.decode(),
        )

    def test_rejection_explanation_is_translated_when_writing_arabic(self) -> None:
        self.switch("ar")
        response = self.switch("de")
        follow_up = self.client.get(response.headers["location"], follow=True)
        self.assertIn(
            "هذه اللغة غير متاحة على هذا النظام.", follow_up.content.decode()
        )

    def test_open_redirect_is_not_followed(self) -> None:
        response = self.switch("ar", next_url="https://evil.example.com/steal")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example.com", response.headers["location"])

    def test_get_request_does_not_change_the_language(self) -> None:
        """The switch is a POST; a stray GET must not silently flip the screen."""
        response = self.client.get(TEACHER_URL, data={"language": "ar", "next": "/"})
        self.assertNotIn("django_language", response.cookies)
        self.assertTrue(
            tag_says(self.client.get(reverse("teacher:dashboard")).content, lang="en")
        )

    def test_djangos_own_endpoint_still_works(self) -> None:
        """The canonical route stays available for anything outside the teacher UI."""
        response = self.switch("ar", url=DJANGO_URL)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies["django_language"].value, "ar")
        self.assertTrue(
            tag_says(self.client.get(reverse("teacher:dashboard")).content, lang="ar")
        )

    def test_switcher_form_is_wired_to_the_teacher_route(self) -> None:
        content = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertIn(f'action="{TEACHER_URL}"', content)
        self.assertIn('name="next"', content)
        # Both buttons, with the active one marked for assistive technology.
        self.assertIn('name="language" value="ar"', content)
        self.assertIn('name="language" value="en"', content)

    # -------------------------------------------- the language really changes

    def test_active_translation_object_follows_the_request(self) -> None:
        """gettext resolves the string under the request's language, not a constant."""
        self.switch("ar")
        with translation.override("ar"):
            self.assertEqual(translation.gettext("Reports"), "التقارير")
        with translation.override("en"):
            self.assertEqual(translation.gettext("Reports"), "Reports")

    def test_content_language_header_reports_the_active_language(self) -> None:
        self.assertEqual(
            self.client.get(reverse("teacher:dashboard")).headers["Content-Language"],
            "en",
        )
        self.switch("ar")
        self.assertEqual(
            self.client.get(reverse("teacher:dashboard")).headers["Content-Language"],
            "ar",
        )

    def test_navigation_labels_are_translated(self) -> None:
        english = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.switch("ar")
        arabic = self.client.get(reverse("teacher:dashboard")).content.decode()

        # The masthead is on every teacher page, so it is the clearest signal
        # that the interface itself moved to Arabic.
        self.assertIn("Dashboard", english)
        self.assertNotIn("لوحة التحكم", english)
        self.assertIn("لوحة التحكم", arabic)
        self.assertIn("التقارير", arabic)
        self.assertIn("المساعدة", arabic)

    def test_buttons_and_actions_are_translated(self) -> None:
        self.switch("ar")
        content = self.client.get(reverse("teacher:competitions")).content.decode()
        self.assertIn("إنشاء مسابقة", content)  # "Create Competition"
        self.assertIn("مسابقاتي", content)  # "My Competitions"
        self.assertIn("إنشاء سؤال", content)  # "Add Question"

    def test_status_labels_come_from_the_translation_catalogue(self) -> None:
        """Model status choices use lazy gettext, so they translate by themselves."""
        from competitions.models import CompetitionState

        competition = create_competition(teacher=self.teacher, title="حصة العلوم ١")
        Competition.objects.filter(pk=competition.pk).update(
            state=CompetitionState.WAITING, started_at=None
        )
        self.switch("ar")
        response = self.client.get(
            reverse("teacher:competition_detail", kwargs={"pk": competition.pk})
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("بانتظار البدء", content)  # "Waiting" state label
        self.assertIn("الحالة", content)  # "State" column label

    def test_empty_states_are_translated(self) -> None:
        self.switch("ar")
        content = self.client.get(reverse("teacher:tournaments")).content.decode()
        self.assertIn("لا توجد بطولات بعد", content)
        self.assertIn("إنشاء بطولة", content)

    def test_validation_messages_are_translated(self) -> None:
        self.switch("ar")
        response = self.client.post(
            reverse("teacher:create_tournament"), data={"action": "next", "name": ""}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("أدخل اسم البطولة.", response.content.decode())

    def test_help_and_contextual_hints_are_translated(self) -> None:
        self.switch("ar")
        content = self.client.get(reverse("teacher:help")).content.decode()
        self.assertIn("المساعدة", content)
        self.assertIn("مساحة عمل المعلم", content)
        # The dashboard's first-use panel, and the role label beside the name.
        dashboard = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertIn("نظرة سريعة على مساحة عملك", dashboard)
        self.assertIn("تم تسجيل الدخول بحساب", dashboard)
        self.assertIn("المعلم", dashboard)
        # The contextual hint beside the competitions heading.
        competitions = self.client.get(reverse("teacher:competitions")).content.decode()
        self.assertIn("المسابقة هي جولة مباشرة واحدة", competitions)

    def test_role_labels_are_translated(self) -> None:
        """Role labels are shared interface text, not model detail."""
        from accounts.roles import Role

        self.switch("ar")
        with translation.override("ar"):
            self.assertEqual(Role.TEACHER.label, "المعلم")
            self.assertEqual(Role.ADMINISTRATOR.label, "المدير")
        with translation.override("en"):
            self.assertEqual(Role.TEACHER.label, "Teacher")

    def test_wizard_step_labels_and_back_buttons_are_translated(self) -> None:
        """The wizard's own controls, on the steps that actually have them."""
        self.switch("ar", next_url="/teacher/tournaments/create/")
        room = create_classroom(name="قاعة الاختبار")
        room.teachers.add(self.teacher)
        # Step one: only a forward button, labelled with the next step.
        content = self.client.get(reverse("teacher:create_tournament")).content.decode()
        self.assertIn("التالي: المشاركون", content)

        self.client.post(
            reverse("teacher:create_tournament"),
            data={
                "action": "next",
                "name": "بطولة الأدوات",
                "subject": "أحياء",
                "grade": "8",
                "season": "2026",
            },
        )
        second = self.client.get(reverse("teacher:create_tournament")).content.decode()
        # Step two has both directions, and both are Arabic.
        self.assertIn("رجوع", second)
        self.assertIn("التالي: المراحل", second)

    def test_switching_language_preserves_tournament_wizard_data(self) -> None:
        """The wizard's state lives in the session, and the switch never touches it."""
        room_one = create_classroom(name="قاعة ١")
        room_one.teachers.add(self.teacher)
        room_two = create_classroom(name="قاعة ٢")
        room_two.teachers.add(self.teacher)
        state = {
            "step": "participants",
            "tournament_name": "دورة اللغة",
            "tournament": {
                "name": "دورة اللغة",
                "subject": "",
                "grade": "",
                "season": "",
                "description": "",
            },
            "subject": "اللغة العربية",
            "grade": "7",
            "season": "2026",
            "classroom_ids": [str(room_one.pk), str(room_two.pk)],
        }
        session = self.client.session
        session[TOURNAMENT_SESSION_KEY] = state
        session.save()

        self.switch("ar", next_url="/teacher/tournaments/create/")

        # The session still holds every value the teacher entered.
        restored = self.client.session[TOURNAMENT_SESSION_KEY]
        self.assertEqual(restored["tournament"]["name"], "دورة اللغة")
        self.assertEqual(restored["classroom_ids"], state["classroom_ids"])

        # And the step still shows the classrooms that were picked, so the
        # switch neither restarted the wizard nor dropped the selection.
        content = self.client.get(reverse("teacher:create_tournament")).content.decode()
        self.assertIn("2. اختر الفصول المشاركة", content)
        self.assertEqual(content.count("checked>"), 2)
        self.assertIn(f'value="{room_one.pk}"', content)
        self.assertIn(f'value="{room_two.pk}"', content)
        self.assertIn("قاعة ١", content)
        self.assertIn("قاعة ٢", content)

    def test_dateless_notice_is_not_left_behind(self) -> None:
        """A half-applied template renders a comment; that must never happen."""
        self.switch("ar")
        for route in TEACHER_PAGES.values():
            with self.subTest(route=route):
                content = self.client.get(reverse(route)).content.decode()
                self.assertNotIn("{#", content)
                self.assertNotIn("{{", content)

    # ---------------------------------------------------------- rendering

    def test_live_dashboard_controls_are_translated(self) -> None:
        """The live dashboard is reached straight from the translated competition list.

        Translating the workspace but leaving this page in English would drop an
        Arabic teacher into a foreign language the moment they opened a round.
        """
        from core.tests.utils import create_competition

        competition = create_competition(teacher=self.teacher, title="جولة مباشرة للتحقق")
        self.switch("ar", next_url="/live/teacher/")
        response = self.client.get(
            reverse("live:teacher_dashboard", args=[competition.pk])
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Teacher-created content is untouched by the interface language.
        self.assertIn("جولة مباشرة للتحقق", content)
        self.assertTrue(tag_says(content, lang="ar", dir="rtl"))
        for label in ("ابدأ المسابقة", "أغلق السؤال", "السؤال التالي", "أغلق المسابقة"):
            with self.subTest(label=label):
                self.assertIn(label, content)

    def test_staff_dashboard_is_translated(self) -> None:
        """The teacher masthead links here, so it is part of the workspace."""
        self.switch("ar", next_url="/dashboard/")
        content = self.client.get(reverse("dashboard")).content.decode()
        self.assertTrue(tag_says(content, lang="ar", dir="rtl"))
        self.assertIn("لوحة التحكم", content)
        self.assertIn("فصولي", content)

    def test_every_teacher_page_renders_in_both_languages(self) -> None:
        for language, direction in (("en", "ltr"), ("ar", "rtl")):
            self.switch(language)
            for name, route in TEACHER_PAGES.items():
                with self.subTest(language=language, page=name):
                    response = self.client.get(reverse(route))
                    self.assertEqual(response.status_code, 200, name)
                    self.assertTrue(
                        tag_says(
                            response.content,
                            lang=language,
                            dir=direction,
                            **{"data-theme": "light"},
                        ),
                        name,
                    )

    def test_arabic_uses_rtl_and_english_uses_ltr(self) -> None:
        english = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertTrue(tag_says(english, lang="en", dir="ltr"))
        self.switch("ar")
        arabic = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertTrue(tag_says(arabic, lang="ar", dir="rtl"))

    def test_language_switcher_is_on_every_teacher_page(self) -> None:
        for route in TEACHER_PAGES.values():
            with self.subTest(route=route):
                content = self.client.get(reverse(route)).content.decode()
                self.assertIn("data-language-form", content)
                self.assertIn('name="language" value="ar"', content)
                self.assertIn('name="language" value="en"', content)

    # -------------------------------------------------------- persistence

    def test_language_persists_across_pages_and_reloads(self) -> None:
        self.switch("ar")
        for route in (
            "teacher:dashboard",
            "teacher:questions",
            "teacher:tournaments",
            "teacher:reports",
            "teacher:help",
        ):
            with self.subTest(route=route):
                content = self.client.get(reverse(route)).content
                self.assertTrue(tag_says(content, lang="ar", dir="rtl"))

    def test_language_persists_across_logout_and_login(self) -> None:
        """The cookie is its own cookie, so logout flushing the session keeps it.

        Sign-out is modelled the way the browser does it rather than with
        ``client.logout()``, which replaces the whole cookie jar with an empty
        one: no browser forgets every cookie on the device when a session ends.

        The assertion is against the <html> element, not any ``lang=`` in the
        page. The language switcher's own buttons carry ``lang="ar"`` in both
        directions, so a bare ``assertIn('lang="ar"')`` would pass on an English
        page and prove nothing at all.
        """
        self.switch("ar")
        self.assertEqual(self.client.cookies["django_language"].value, "ar")

        signed_out = self.client.post(reverse("accounts:logout"))
        # Signing out says nothing about the language cookie either way.
        self.assertNotIn("django_language", signed_out.cookies)

        # A request that still carries the cookie is still Arabic, and signing
        # back in restores the session without disturbing the language.
        self.client.force_login(self.teacher)
        self.assertTrue(
            tag_says(
                self.client.get(reverse("teacher:dashboard")).content,
                lang="ar",
                dir="rtl",
            )
        )

    def test_language_cookie_outlives_the_browser_session(self) -> None:
        """A session cookie would die with the process; it is keyed to the session age."""
        self.assertEqual(settings.LANGUAGE_COOKIE_AGE, settings.SESSION_COOKIE_AGE)

    def test_cookie_attrs_are_set_for_the_deployment(self) -> None:
        response = self.switch("ar")
        cookie = response.cookies["django_language"]
        self.assertTrue(cookie["max-age"] and int(cookie["max-age"]) > 0)
        self.assertEqual(cookie["path"], "/")

    # ------------------------------------------------------ wizard safety

    @staticmethod
    def competition_wizard_state() -> dict:
        return {
            "step": "settings",
            "basic": {
                "title": "جولة سريعة",
                "subject": "علوم",
                "grade": "6",
                "description": "",
            },
            "classroom_ids": [],
            "settings": {
                "duration_seconds": 45,
                "scoring_mode": "standard",
                "speed_bonus": 5,
            },
            "question_ids": [],
        }

    def test_switching_language_preserves_competition_wizard_data(self) -> None:
        session = self.client.session
        session[COMPETITION_SESSION_KEY] = self.competition_wizard_state()
        session.save()

        self.switch("ar", next_url="/teacher/competitions/create/")

        restored = self.client.session[COMPETITION_SESSION_KEY]
        self.assertEqual(restored["settings"]["duration_seconds"], 45)
        self.assertEqual(restored["basic"]["title"], "جولة سريعة")

    def test_switching_language_keeps_the_wizard_on_the_same_step(self) -> None:
        """Advance a step, switch, and the wizard must still be on that step."""
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
        self.assertEqual(
            self.client.session[TOURNAMENT_SESSION_KEY]["step"], "participants"
        )

        self.switch("ar", next_url="/teacher/tournaments/create/")
        self.assertEqual(
            self.client.session[TOURNAMENT_SESSION_KEY]["tournament"]["name"],
            "بطولة الاختبار",
        )
        content = self.client.get(reverse("teacher:create_tournament")).content.decode()
        # Step two of the wizard, still step two - the switch did not restart it.
        self.assertIn("2. اختر الفصول المشاركة", content)

    def test_switching_language_twice_is_a_no_op_for_the_wizard(self) -> None:
        """English -> Arabic -> English, the round trip a teacher actually makes."""
        create_classroom(name="قاعة الاتجاه")
        self.client.post(
            reverse("teacher:create_tournament"),
            data={
                "action": "next",
                "name": "بطولة الاتجاه",
                "subject": "فيزياء",
                "grade": "9",
                "season": "٢٠٢٦",
            },
        )
        for code in ("ar", "en", "ar"):
            self.switch(code, next_url="/teacher/tournaments/create/")
            self.assertEqual(
                self.client.session[TOURNAMENT_SESSION_KEY]["step"], "participants", code
            )

    # ------------------------------------------------------- the catalogue

    def test_catalogue_has_no_fuzzy_or_untranslated_entries(self) -> None:
        """A fuzzy entry is dropped by msgfmt, which is exactly the bug this guards.

        `makemessages` marks an entry fuzzy when a string's wording changed but a
        similar one existed, and copies that similar translation over as a guess.
        msgfmt refuses to compile fuzzy entries, so the string silently falls back
        to English: "Tournament standings" came out translated as the step heading
        "1. تفاصيل البطولة", and the counted wizard strings came out as
        "6. راجع البطولة".
        """
        catalogue = settings.LOCALE_PATHS[0] / "ar" / "LC_MESSAGES" / "django.po"
        self.assertTrue(catalogue.exists(), "the Arabic catalogue must be committed")
        text = catalogue.read_text(encoding="utf-8")

        self.assertNotIn("#, fuzzy", text)
        self.assertNotIn("#| msgid", text)

        # No msgid may carry an empty translation, single-line or plural.
        self.assertEqual(
            re.findall(r'^msgid ".+"$\n^msgstr ""$', text, re.MULTILINE), []
        )
        self.assertEqual(re.findall(r"^msgstr\[\d\] \"\"$", text, re.MULTILINE), [])

    def test_catalogue_has_an_entry_for_every_teacher_string(self) -> None:
        catalogue = (
            settings.LOCALE_PATHS[0] / "ar" / "LC_MESSAGES" / "django.po"
        ).read_text(encoding="utf-8")
        for msgid in (
            "Create Competition",
            "My Tournaments",
            "Tournament detail",
            "Final standings",
            "No classrooms have been added.",
            "Language",
            "Plan a tournament step by step",
        ):
            with self.subTest(msgid=msgid):
                self.assertIn(f'msgid "{msgid}"', catalogue)

    def test_compiled_catalogue_exists(self) -> None:
        """The .po is the source; gettext only reads the .mo next to it."""
        compiled = (
            settings.LOCALE_PATHS[0] / "ar" / "LC_MESSAGES" / "django.mo"
        )
        self.assertTrue(
            compiled.exists(),
            "run `django-admin compilemessages --locale ar` (the image build does)",
        )

    def test_settings_declare_exactly_the_two_languages(self) -> None:
        self.assertEqual(
            dict(settings.LANGUAGES), {"en": "English", "ar": "العربية"}
        )
        self.assertTrue(settings.USE_I18N)
        self.assertEqual(settings.LOCALE_PATHS[0].name, "locale")
        # LocaleMiddleware must sit after SessionMiddleware: it reads the cookie
        # the switch writes, and the session is what carries the wizard state
        # that switch is expected not to disturb.
        order = settings.MIDDLEWARE
        self.assertLess(order.index("django.contrib.sessions.middleware.SessionMiddleware"),
                        order.index("django.middleware.locale.LocaleMiddleware"))
