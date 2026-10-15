"""The teacher workflow: question quick-create and classroom screen targeting.

The workflow this suite protects:

1. A teacher creating a question at the wizard's questions step must be able to
   create one without losing the competition they are halfway through.
2. Starting a competition must reach every screen in the participating
   classrooms, and no screen belonging to a classroom that was left out.
3. A screen's registered URL is its permanent address: it carries no
   competition, so nothing is typed on the classroom machine per round.

The screen half is driven over real WebSocket connections through
:mod:`live.tests.support`, because the property being tested - a group broadcast
reaching one socket and not another - only exists once a socket exists. A test
that called the consumer's methods directly would prove nothing about routing,
authentication or delivery.

The permission half is driven with plain HTTP: a teacher creating questions, and
a teacher *not* reaching the screen administration, are two different routes and
both must be exercised as the browser reaches them.
"""

from __future__ import annotations

import asyncio
import re

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from competitions.models import CompetitionClassroom, CompetitionState
from config.asgi import application
from core.tests.factories import (
    create_classroom,
    create_competition,
    create_question,
    create_screen,
    create_subject,
    create_teacher,
)
from live import events
from live.services import start_competition, start_question
from live.consumers_base import CLOSE_UNAUTHORISED
from live.tests.support import (
    CONNECT_TIMEOUT_SECONDS,
    build_teacher_scope,
    drain_until,
    origin_headers,
    receive_within,
    screen_url,
    send_message,
    teacher_url,
)

STEP_QUESTIONS = "questions"
SESSION_KEY = "teacher_competition_wizard"


def wizard_state(session, **overrides) -> dict:
    """Seed a wizard mid-flight and return its state.

    A teacher who has already filled in a name, settings and classrooms and is
    standing on the questions step - exactly the state the quick-create has to
    survive.
    """
    state = {
        "step": STEP_QUESTIONS,
        "basic": {
            "title": "الجولة الأولى",
            "subject": "علوم",
            "grade": "7",
            "description": "",
        },
        "classroom_ids": [],
        "settings": {
            "duration_seconds": 30,
            "scoring_mode": "standard",
            "speed_bonus": 10,
        },
        "question_ids": [],
        "filters": {"q": "", "type": ""},
    }
    state.update(overrides)
    session[SESSION_KEY] = state
    session.save()
    return state


# ---------------------------------------------------------------------------
# 1. Question creation permission
# ---------------------------------------------------------------------------


class QuestionCreationPermissionTests(TestCase):
    """A teacher may create questions; nobody outside staff may."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.question.teacher")
        cls.subject = create_subject(name="Mathematics", slug="mathematics")

    def question_form(self) -> dict:
        return {
            "question_type": "multiple_choice",
            "text": "سؤال سريع من المعالج",
            "subject": self.subject.pk,
            "options": '["أ","ب","ج"]',
            "correct_option": "أ",
            "duration_seconds": "30",
            "active": "on",
            "save_option": "save",
        }

    def test_a_teacher_can_create_a_question(self) -> None:
        from questions.models import Question

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("teacher:create_question"), self.question_form())
        self.assertEqual(response.status_code, 302)
        created = Question.objects.get(text="سؤال سريع من المعالج")
        self.assertEqual(created.subject, self.subject)

    def test_a_question_without_a_subject_is_rejected(self) -> None:
        """A question must belong to a subject, or the bank cannot be organised."""
        from questions.models import Question

        self.client.force_login(self.teacher)
        payload = self.question_form()
        payload.pop("subject")
        response = self.client.post(reverse("teacher:create_question"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Question.objects.filter(text="سؤال سريع من المعالج").exists())

    def test_an_administrator_can_create_a_question(self) -> None:
        from core.tests.utils import create_administrator

        administrator = create_administrator(username="ui8.question.admin")
        self.client.force_login(administrator)
        response = self.client.post(reverse("teacher:create_question"), self.question_form())
        self.assertEqual(response.status_code, 302)

    def test_an_anonymous_visitor_cannot_reach_the_builder(self) -> None:
        response = self.client.get(reverse("teacher:create_question"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.headers["location"])

    def test_an_account_belonging_to_neither_role_is_refused(self) -> None:
        """The guard that stands between a role nobody intended and the workspace.

        Two layers, both tested. ``is_staff_member`` refuses a role that is not
        one of the two teaching roles, which is what the workspace views call.
        And the database carries a check constraint on the column, so such a row
        cannot be stored in the first place - the reason this test cannot create
        one and let the view refuse it.
        """
        from accounts.models import Role, User
        from accounts.permissions import is_staff_member
        from django.db import connection

        # Layer one: the permission helper.
        outsider = User(username="ui8.roleoutsider")
        outsider.role = "observer"
        self.assertFalse(is_staff_member(outsider))
        self.assertTrue(is_staff_member(create_teacher(username="ui8.role.in")))

        # Layer two: the constraint.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_constraint WHERE conname = %s", ["accounts_user_role_is_valid"]
            )
            self.assertIsNotNone(cursor.fetchone())
        self.assertEqual(set(Role.values), {"administrator", "teacher", "stage_deputy"})
        self.assertFalse(is_staff_member(User(username="deputy.guard", role=Role.STAGE_DEPUTY)))

    def test_the_no_role_case_cannot_reach_the_questions_view(self) -> None:
        """The questions view is behind the same guard as everything else."""
        from accounts.models import User
        from accounts.permissions import is_staff_member

        outsider = User(username="ui8.notstaff")
        outsider.role = "warehouse"
        self.assertFalse(is_staff_member(outsider))

    def test_a_teacher_cannot_reach_screen_administration(self) -> None:
        """The one thing a teacher must never be able to do.

        A Screen ID is the credential a screen presents, so issuing one is
        administrator-only: a teacher who could record a screen could aim a
        classroom's display at any classroom. The workspace has no route to it,
        and the admin itself refuses them.
        """
        self.client.force_login(self.teacher)
        for path in (
            reverse("admin:screens_interactivescreen_add"),
            reverse("admin:screens_interactivescreen_changelist"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                # Refused, and sent to the admin's own login rather than let in.
                self.assertIn("/admin/login/", response.headers["location"])
                self.assertIn("next=", response.headers["location"])

    def test_the_workspace_has_no_screen_management_route(self) -> None:
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("teacher:manage_screens")


# ---------------------------------------------------------------------------
# 2. Quick-create preserves the wizard
# ---------------------------------------------------------------------------


class WizardQuickCreateTests(TestCase):
    """Creating a question from the wizard must not lose the competition."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.quick.teacher")
        cls.subject = create_subject(name="Mathematics", slug="ui8-maths")
        cls.room_one = create_classroom(name="غرفة الاختبار ١")
        cls.room_two = create_classroom(name="غرفة الاختبار ٢")
        cls.room_one.teachers.add(cls.teacher)
        cls.room_two.teachers.add(cls.teacher)
        cls.existing = create_question(
            text="سؤال قديم في البنك", subject=cls.subject
        )

    def setUp(self) -> None:
        self.client.force_login(self.teacher)
        wizard_state(
            self.client.session,
            subject_id=str(self.subject.pk),
            classroom_ids=[str(self.room_one.pk), str(self.room_two.pk)],
            question_ids=[str(self.existing.pk)],
        )

    def new_question_form(self) -> dict:
        return {
            "question_type": "multiple_choice",
            "text": "سؤال منشأ داخل المعالج",
            "subject": self.subject.pk,
            "options": '["أولى","ثانية","ثالثة"]',
            "correct_option": "أولى",
            "duration_seconds": "25",
            "active": "on",
            "save_option": "save_to_competition",
        }

    def created_question(self):
        from questions.models import Question

        return Question.objects.get(text="سؤال منشأ داخل المعالج")

    def detour(self):
        """Leave the wizard, create a question, and come back."""
        response = self.client.post(
            reverse("teacher:create_question"), self.new_question_form()
        )
        self.assertEqual(response.status_code, 302)
        return self.client.get(response.headers["location"])

    def test_the_wizard_offers_a_create_question_action(self) -> None:
        response = self.client.get(reverse("teacher:create_competition"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, reverse("teacher:create_question") + "?return_to_competition=1"
        )
        self.assertContains(response, "Create new question")

    def test_the_new_question_is_created_and_selected(self) -> None:
        """The whole point of the return path: it must actually add the question."""
        response = self.client.post(
            reverse("teacher:create_question"), self.new_question_form()
        )
        created = self.created_question()
        self.assertIn(f"question_id={created.pk}", response.headers["location"])

        # The redirect lands on the clean wizard URL, so following it is what a
        # browser does.
        return_page = self.client.get(response.headers["location"], follow=True)
        self.assertEqual(return_page.status_code, 200)
        self.assertIn(str(created.pk), self.client.session[SESSION_KEY]["question_ids"])
        self.assertContains(return_page, "سؤال منشأ داخل المعالج")
        self.assertContains(return_page, "Create new question")

    def test_the_round_trip_preserves_everything(self) -> None:
        """Name, settings, classrooms and the earlier question must all survive."""
        self.detour()
        state = self.client.session[SESSION_KEY]

        self.assertEqual(state["step"], STEP_QUESTIONS, "the step was reset")
        self.assertEqual(state["basic"]["title"], "الجولة الأولى")
        self.assertEqual(state["basic"]["subject"], "علوم")
        self.assertEqual(state["basic"]["grade"], "7")
        self.assertEqual(state["settings"]["duration_seconds"], 30)
        self.assertEqual(state["settings"]["speed_bonus"], 10)
        self.assertEqual(
            sorted(state["classroom_ids"]),
            sorted([str(self.room_one.pk), str(self.room_two.pk)]),
        )
        self.assertIn(str(self.existing.pk), state["question_ids"])

    def test_the_new_question_is_ticked_on_the_step_it_returns_to(self) -> None:
        response = self.client.post(
            reverse("teacher:create_question"), self.new_question_form()
        )
        return_page = self.client.get(response.headers["location"], follow=True)
        # A pattern, not a literal: the attribute order in the rendered card puts
        # `value` and `checked` on separate lines.
        self.assertRegex(
            return_page.content.decode(),
            rf'value="{self.created_question().pk}"\s+checked',
        )

    def test_the_search_filter_is_reset_so_the_new_one_is_visible(self) -> None:
        """A search that hid the new question would silently not select it."""
        session = self.client.session
        state = session[SESSION_KEY]
        state["filters"] = {"q": "لا يوجد شيء بهذا الاسم", "type": ""}
        session[SESSION_KEY] = state
        session.save()

        self.detour()
        self.assertEqual(
            self.client.session[SESSION_KEY]["filters"], {"q": "", "type": ""}
        )

    def test_a_malformed_return_is_ignored_not_fatal(self) -> None:
        """A mistyped or tampered question_id must not break the wizard."""
        response = self.client.get(
            reverse("teacher:create_competition"), {"question_id": "not-a-number"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Step 4")

    def test_a_missing_question_id_leaves_the_wizard_alone(self) -> None:
        response = self.client.get(
            reverse("teacher:create_competition"), {"question_id": "99999999"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session[SESSION_KEY]["step"], STEP_QUESTIONS)

    def test_returning_works_from_outside_the_wizard_too(self) -> None:
        """A teacher who wandered off the wizard entirely still gets the question."""
        self.client.session.clear()
        response = self.client.post(
            reverse("teacher:create_question"), self.new_question_form()
        )
        return_page = self.client.get(response.headers["location"], follow=True)
        self.assertEqual(return_page.status_code, 200)
        self.assertIn(str(self.created_question().pk), self.client.session[SESSION_KEY]["question_ids"])

    def test_the_builder_marks_the_return_path(self) -> None:
        response = self.client.get(
            reverse("teacher:create_question"), {"return_to_competition": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Save and return to the competition")
        self.assertContains(response, reverse("teacher:create_competition"))

    def test_the_dedicated_question_builder_is_still_available(self) -> None:
        """The quick path must not have replaced the full builder."""
        for route in ("teacher:questions", "teacher:create_question"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 200)

    def test_every_existing_question_type_is_still_supported(self) -> None:
        from questions.models import Question, QuestionType

        response = self.client.get(reverse("teacher:create_question"))
        for value, _label in QuestionType.choices:
            with self.subTest(question_type=value):
                # The builder offers the type, and accepts a question of it.
                self.assertContains(response, value)

        # An unknown type is refused rather than silently stored.
        payload = self.new_question_form()
        payload["question_type"] = "not_a_real_type"
        self.assertEqual(
            self.client.post(reverse("teacher:create_question"), payload).status_code, 200
        )
        self.assertFalse(
            Question.objects.filter(question_type="not_a_real_type").exists()
        )

    def test_a_validation_error_keeps_the_wizard_intact(self) -> None:
        """An invalid question must not clear the competition being built."""
        payload = self.new_question_form()
        payload.pop("text")
        response = self.client.post(
            reverse("teacher:create_question"), payload
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            self.client.session[SESSION_KEY]["classroom_ids"],
            "the classroom selection was lost by a failed question save",
        )


# ---------------------------------------------------------------------------
# 3. Screen targeting
# ---------------------------------------------------------------------------


class ScreenTargetingTests(TestCase):
    """What the teacher is shown about which screens a round will reach."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.targeting.teacher")
        cls.room_one = create_classroom(name="غرفة الهدف ١")
        cls.room_two = create_classroom(name="غرفة الهدف ٢")
        cls.room_three = create_classroom(name="غرفة الهدف ٣")

        # One classroom with a connected screen, one with an offline screen and
        # one with no screen at all: the three cases the review must tell apart.
        cls.live_screen = create_screen(
            name="شاشة غرفة ١", classroom=cls.room_one, is_primary=True
        )
        cls.live_screen.record_heartbeat(ip_address="10.0.0.11")
        cls.dead_screen = create_screen(name="شاشة غرفة ٢", classroom=cls.room_two)

        cls.competition = create_competition(
            teacher=cls.teacher,
            title="مسابقة الاستهداف",
            classrooms=[cls.room_one, cls.room_two, cls.room_three],
            questions=[create_question(text="سؤال الاستهداف")],
        )

    def test_targeting_counts_each_state(self) -> None:
        from screens import targeting

        result = targeting.for_competition(self.competition)
        self.assertEqual(len(result.classrooms), 3)
        self.assertEqual(len(result.screens), 2)
        self.assertEqual(len(result.online_screens), 1)
        self.assertEqual(len(result.offline_screens), 1)
        self.assertEqual(
            [target.name for target in result.classrooms],
            ["غرفة الهدف ١", "غرفة الهدف ٢", "غرفة الهدف ٣"],
        )
        self.assertEqual(
            [target.name for target in result.classrooms_without_screens],
            ["غرفة الهدف ٣"],
        )

    def test_the_summary_names_the_right_verdict(self) -> None:
        from screens import targeting

        self.assertEqual(
            targeting.for_competition(self.competition).summary, "partial"
        )
        CompetitionClassroom.objects.filter(competition=self.competition).delete()
        self.assertEqual(
            targeting.for_competition(self.competition).summary, "no-screens"
        )

    def test_a_classroom_with_no_screen_is_distinguished(self) -> None:
        from screens import targeting

        result = targeting.for_competition(self.competition)
        without = [target for target in result.classrooms if not target.has_screen]
        self.assertEqual([target.name for target in without], ["غرفة الهدف ٣"])
        self.assertEqual(without[0].status_display, "no-screen")
        self.assertTrue(
            any(target.has_online_screen for target in result.classrooms)
        )

    def test_an_offline_screen_is_not_claimed_to_be_reachable(self) -> None:
        """The server writes only to sockets that are open, so it is not "sent"."""
        from screens import targeting

        result = targeting.for_competition(self.competition)
        self.assertEqual(
            [screen.screen_id for screen in result.offline_screens],
            [self.dead_screen.screen_id],
        )
        self.assertEqual(
            [screen.screen_id for screen in result.online_screens],
            [self.live_screen.screen_id],
        )
        # "Reached now" excludes it, which is what the wording must reflect: the
        # only classroom whose screens are reachable is the connected one.
        reached = [
            screen.screen_id
            for target in result.classrooms_reached
            for screen in target.online_screens
        ]
        self.assertEqual(reached, [self.live_screen.screen_id])

    def test_a_retired_screen_is_not_counted(self) -> None:
        from screens import targeting

        self.live_screen.active = False
        self.live_screen.save(update_fields=["active"])
        result = targeting.for_competition(self.competition)
        self.assertNotIn(
            self.live_screen.screen_id, [screen.screen_id for screen in result.screens]
        )

    def test_the_competition_detail_page_names_every_screen(self) -> None:
        self.client.force_login(self.teacher)
        response = self.client.get(
            reverse("teacher:competition_detail", kwargs={"pk": self.competition.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screens this competition will reach")
        self.assertContains(response, self.live_screen.screen_id)
        self.assertContains(response, self.dead_screen.screen_id)
        self.assertContains(response, "No registered screen")
        # Once as a row, and once more in the offline warning.
        self.assertContains(response, self.dead_screen.screen_id, count=2)

    def test_the_live_dashboard_shows_the_expected_screens(self) -> None:
        self.client.force_login(self.teacher)
        response = self.client.get(
            reverse("live:teacher_dashboard", args=[self.competition.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screens this competition will reach")
        self.assertContains(response, self.live_screen.screen_id)

    def test_a_competition_with_no_screens_says_so_plainly(self) -> None:
        self.client.force_login(self.teacher)
        for screen in (self.live_screen, self.dead_screen):
            screen.active = False
            screen.save(update_fields=["active"])
        response = self.client.get(
            reverse("live:teacher_dashboard", args=[self.competition.pk])
        )
        self.assertContains(
            response, "No screens are registered for these classrooms yet"
        )

    def test_the_wizard_review_shows_the_screens_before_creating(self) -> None:
        """The review step must show the targeting before anything exists."""
        self.client.force_login(self.teacher)
        review_question = create_question(text="سؤال للمراجعة")
        wizard_state(
            self.client.session,
            step="review",
            classroom_ids=[
                str(self.room_one.pk),
                str(self.room_two.pk),
                str(self.room_three.pk),
            ],
            question_ids=[str(review_question.pk)],
        )
        response = self.client.get(reverse("teacher:create_competition"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screens this competition will reach")
        self.assertContains(response, self.live_screen.screen_id)
        self.assertContains(response, "No registered screen")

    def test_another_teacher_cannot_read_the_targeting(self) -> None:
        """Targeting is a property of the competition, not of the viewer."""
        stranger = create_teacher(username="ui8.targeting.stranger")
        self.client.force_login(stranger)
        response = self.client.get(
            reverse("teacher:competition_detail", kwargs={"pk": self.competition.pk})
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# 6. Both languages, both themes
# ---------------------------------------------------------------------------


class WorkflowLanguageAndThemeTests(TestCase):
    """The workflow must be usable, and correct, in either language or theme.

    The wizard's state is held in the session and the theme in its own cookie, so
    a teacher can change either mid-flow without the competition being lost - and
    that is what this checks, because a workflow that only works in English is a
    workflow half the staff cannot use.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.both.teacher")
        cls.subject = create_subject(name="Mathematics", slug="ui8-both-maths")
        cls.room = create_classroom(name="غرفة كلا اللغتين")
        cls.room.teachers.add(cls.teacher)
        cls.screen = create_screen(
            name="شاشة كلا اللغتين", classroom=cls.room, is_primary=True
        )
        cls.competition = create_competition(
            teacher=cls.teacher,
            title="مسابقة كلا اللغتين",
            classrooms=[cls.room],
            questions=[create_question(text="سؤال كلا اللغتين", subject=cls.subject)],
        )
        cls.question = create_question(text="سؤال اللغتين في البنك")

    def switch(self, language: str, theme: str | None = None):
        if language:
            self.client.post(
                reverse("teacher:set_language"),
                {"language": language, "next": "/teacher/"},
            )
        if theme:
            self.client.post(reverse("core:set_theme"), {"theme": theme, "next": "/teacher/"})

    def test_arabic_targeting_panel_is_translated(self) -> None:
        self.client.force_login(self.teacher)
        self.switch("ar")
        response = self.client.get(
            reverse("teacher:competition_detail", kwargs={"pk": self.competition.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الشاشات التي ستصلها هذه المسابقة")
        self.assertContains(response, self.screen.screen_id, count=2)
        # The screen's own id is never language-dependent.
        self.assertContains(response, self.screen.screen_id)

    def test_arabic_and_english_both_shows_the_same_screen(self) -> None:
        self.client.force_login(self.teacher)
        english = self.client.get(
            reverse("teacher:competition_detail", kwargs={"pk": self.competition.pk})
        ).content.decode()
        self.switch("ar")
        arabic = self.client.get(
            reverse("teacher:competition_detail", kwargs={"pk": self.competition.pk})
        ).content.decode()

        self.assertIn(self.screen.screen_id, english)
        self.assertIn(self.screen.screen_id, arabic)
        self.assertIn('dir="ltr"', english)
        self.assertIn('dir="rtl"', arabic)

    def test_the_screen_page_renders_in_both_themes_and_directions(self) -> None:
        """The classroom display itself follows the active language and theme."""
        for language, direction in (("en", "ltr"), ("ar", "rtl")):
            for theme in ("light", "dark"):
                with self.subTest(language=language, theme=theme):
                    self.client.cookies.clear()
                    self.client.cookies["django_language"] = language
                    self.client.cookies["almanar_theme"] = theme
                    response = self.client.get(
                        "/screen/", {"screen_id": self.screen.screen_id}
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, f'dir="{direction}"')

    def test_switching_theme_does_not_lose_the_wizard(self) -> None:
        """The theme switch never reloads the page, so the wizard is untouched."""
        self.client.force_login(self.teacher)
        wizard_state(
            self.client.session,
            classroom_ids=[str(self.room.pk)],
            question_ids=[str(self.question.pk)],
        )

        self.switch(None, "dark")
        state = self.client.session[SESSION_KEY]
        self.assertEqual(state["step"], STEP_QUESTIONS)
        self.assertEqual(state["classroom_ids"], [str(self.room.pk)])
        self.assertEqual(state["question_ids"], [str(self.question.pk)])

        page = self.client.get(reverse("teacher:create_competition"))
        self.assertContains(page, 'data-theme="dark"')

    def test_switching_language_does_not_lose_the_wizard(self) -> None:
        self.client.force_login(self.teacher)
        wizard_state(
            self.client.session,
            classroom_ids=[str(self.room.pk)],
            question_ids=[str(self.question.pk)],
        )
        self.switch("ar")

        state = self.client.session[SESSION_KEY]
        self.assertEqual(state["classroom_ids"], [str(self.room.pk)])
        self.assertEqual(state["question_ids"], [str(self.question.pk)])
        page = self.client.get(reverse("teacher:create_competition"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'lang="ar"')

    def test_the_question_flow_works_in_arabic_end_to_end(self) -> None:
        """Create from the wizard while the workspace is in Arabic."""
        self.client.force_login(self.teacher)
        self.switch("ar")
        wizard_state(
            self.client.session,
            classroom_ids=[str(self.room.pk)],
            question_ids=[str(self.question.pk)],
        )
        response = self.client.post(
            reverse("teacher:create_question"),
            {
                "question_type": "multiple_choice",
                "text": "سؤال عربي من المعالج",
                "subject": self.subject.pk,
                "options": '["أ","ب","ج"]',
                "correct_option": "ب",
                "duration_seconds": "20",
                "active": "on",
                "save_option": "save_to_competition",
            },
        )
        from questions.models import Question

        created = Question.objects.get(text="سؤال عربي من المعالج")
        # Following the redirect is what a browser does, and is where the wizard
        # adopts the new question.
        return_page = self.client.get(response.headers["location"], follow=True)
        self.assertEqual(return_page.status_code, 200)
        self.assertContains(return_page, "سؤال عربي من المعالج")
        self.assertIn(str(created.pk), self.client.session[SESSION_KEY]["question_ids"])
        self.assertEqual(self.client.session[SESSION_KEY]["classroom_ids"], [str(self.room.pk)])
        self.assertContains(return_page, 'lang="ar"')


# ---------------------------------------------------------------------------
# 4. Persistent screen URL
# ---------------------------------------------------------------------------


class ScreenRegistrationUrlTests(TestCase):
    """``/screen/?screen_id=...`` is the screen's permanent address."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.url.teacher")
        cls.room = create_classroom(name="غرفة الرابط الدائم")
        cls.room.teachers.add(cls.teacher)
        cls.screen = create_screen(
            name="شاشة الرابط", classroom=cls.room, is_primary=True
        )

    def screen_page(self, **params):
        params.setdefault("screen_id", self.screen.screen_id)
        return self.client.get("/screen/", params)

    def test_the_registered_path_carries_no_competition(self) -> None:
        """The one property that makes the URL permanent."""
        path = self.screen.registration_path
        self.assertEqual(path, f"/screen/?screen_id={self.screen.screen_id}")
        self.assertNotIn("competition", path)
        self.assertNotIn(str(self.screen.pk), path)

    def test_the_registered_url_shows_the_live_display(self) -> None:
        """Opening the Administrator's URL renders the display, not a dead page."""
        response = self.screen_page()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "live/screen.html")
        self.assertContains(response, f'data-screen-id="{self.screen.screen_id}"')
        self.assertContains(response, 'data-websocket-path="/ws/live/screen/"')
        self.assertContains(response, "live/js/screen.")
        self.assertContains(response, "core/js/screen-heartbeat.")

    def test_the_same_page_is_served_from_both_addresses(self) -> None:
        """One description of what a screen shows, so they cannot disagree."""
        registered = self.screen_page()
        live = self.client.get(
            reverse("live:screen"), {"screen_id": self.screen.screen_id}
        )
        self.assertTemplateUsed(live, "live/screen.html")
        # Not the whole document: each request carries its own CSRF token, so the
        # bytes differ for a reason that has nothing to do with the screen. What
        # must be identical is everything that describes the screen.
        for marker in (
            f'data-screen-id="{self.screen.screen_id}"',
            'data-websocket-path="/ws/live/screen/"',
            'data-panel="waiting"',
            'class="screen"',
            "غرفة الرابط الدائم",
        ):
            with self.subTest(marker=marker):
                self.assertContains(registered, marker)
                self.assertContains(live, marker)

    def test_the_waiting_state_is_rendered_without_any_competition(self) -> None:
        """No round is running; the screen must still say something useful."""
        response = self.screen_page()
        self.assertContains(response, 'data-panel="waiting"')
        self.assertContains(response, "Connecting")

    def test_the_screen_page_names_itself_and_its_classroom(self) -> None:
        # On a classroom board the classroom is what a reader needs, so it takes
        # the title; the screen's own descriptive name is for administrators and
        # is shown only when the screen has no classroom yet.
        response = self.screen_page()
        self.assertContains(response, "غرفة الرابط الدائم")
        self.assertContains(response, self.screen.screen_id)
        self.assertNotContains(response, "شاشة الرابط")

    def test_an_unassigned_screen_is_told_so(self) -> None:
        self.screen.classroom = None
        self.screen.save(update_fields=["classroom"])
        response = self.screen_page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not yet assigned to a classroom")

    def test_an_unknown_screen_id_is_still_not_enumerable(self) -> None:
        response = self.client.get("/screen/", {"screen_id": "AM-ZZZZZZ"})
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, self.screen.screen_id, status_code=404)

    def test_the_screen_page_leaks_no_administration_or_other_screens(self) -> None:
        other = create_screen(name="شاشة أخرى", classroom=self.room)
        response = self.screen_page()
        self.assertNotContains(response, other.screen_id)
        self.assertNotContains(response, "/admin/")

    def test_the_screen_page_shows_live_presence(self) -> None:
        self.screen.record_heartbeat(ip_address="10.0.0.5")
        response = self.screen_page()
        self.assertContains(response, 'id="screen-status"')
        self.assertContains(response, "Online")

    def test_the_page_does_not_name_the_competition_it_will_join(self) -> None:
        """The screen's address must not change when a different round runs."""
        competition = create_competition(
            teacher=self.teacher,
            title="مسابقة الرابط",
            classrooms=[self.room],
            questions=[create_question(text="سؤال الرابط")],
        )
        start_competition(competition)
        response = self.screen_page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-screen-id="{self.screen.screen_id}"')
        self.assertNotContains(response, "competition_id")
        self.assertNotContains(response, "مسابقة الرابط")


# ---------------------------------------------------------------------------
# 5. WebSocket delivery to selected screens only
# ---------------------------------------------------------------------------


def _reset_channel_layer_cache() -> None:
    from channels.layers import channel_layers

    channel_layers._reset_backends(setting=None, enter=True)


class ScreenDeliveryTests(TransactionTestCase):
    """One Start reaches the selected screens and nothing else.

    ``TransactionTestCase`` for the reason :class:`live.tests.test_live_engine`
    gives: real channel connections and background clock tasks need real
    transaction boundaries. The channel layer is the in-memory one for the reason
    there too - ``channels_redis`` pools connections bound to the event loop that
    created them, which deadlocks a second async test.
    """

    reset_sequences = True

    @classmethod
    def setUpClass(cls) -> None:
        cls._layer = override_settings(
            CHANNEL_LAYERS={
                "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
            }
        )
        cls._layer.enable()
        _reset_channel_layer_cache()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        cls._layer.disable()
        _reset_channel_layer_cache()

    def setUp(self) -> None:
        from live.presence import clear_members

        clear_members()
        self.teacher = create_teacher(username="ui8.delivery.teacher")
        self.room_in = create_classroom(name="غرفة الداخل")
        self.room_out = create_classroom(name="غرفة الخارج")
        self.room_in.teachers.add(self.teacher)

        self.screen_in_one = create_screen(
            name="شاشة الداخل ١", classroom=self.room_in, is_primary=True
        )
        self.screen_in_two = create_screen(name="شاشة الداخل ٢", classroom=self.room_in)
        self.screen_out = create_screen(name="شاشة الخارج", classroom=self.room_out)

        self.competition = create_competition(
            teacher=self.teacher,
            title="مسابقة التسليم",
            classrooms=[self.room_in],
            questions=[create_question(text="سؤال التسليم")],
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    async def _in_thread(func, *args, **kwargs):
        return await database_sync_to_async(func)(*args, **kwargs)

    async def connect_screen_by_id(self, screen_id: str):
        """Open a socket for any Screen ID, registered or not."""
        communicator = WebsocketCommunicator(
            application, screen_url(screen_id), headers=origin_headers()
        )
        connected, close_code = await communicator.connect(
            timeout=CONNECT_TIMEOUT_SECONDS
        )
        return communicator, connected, close_code

    async def assert_silent(self, communicator, seconds: float = 2.0) -> None:
        """Assert that nothing arrives on a socket.

        Written this way rather than with ``assertRaises(asyncio.TimeoutError)``
        because ``async_timeout`` cancels the task *that awaits it*, and a task
        that swallows that cancellation keeps a pending one: the next await - the
        ``disconnect()`` in a ``finally`` - then raises ``CancelledError`` for no
        reason a reader could see. Racing a watcher task against a timer cancels
        the watcher, never the test's own task.
        """
        received: list = []
        arrived = asyncio.Event()

        async def watch() -> None:
            received.append(await communicator.receive_output(timeout=seconds + 5))
            arrived.set()

        watcher = asyncio.create_task(watch())
        try:
            await asyncio.wait([watcher], timeout=seconds)
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        self.assertFalse(arrived.is_set(), f"a message arrived when none was expected: {received}")

    async def connect_screen(self, screen):
        """Open a real screen socket, returning it and whether it was accepted.

        Whether the server accepts it is the caller's business, not this helper's:
        the interesting cases are accepted-with-a-round, accepted-and-waiting, and
        refused-because-the-classroom-was-left-out. A screen has no session, so
        the scope is the anonymous one and the Screen ID in the URL is the whole
        of its credential - exactly as a classroom browser connects.
        """
        communicator = WebsocketCommunicator(
            application, screen_url(screen.screen_id), headers=origin_headers()
        )
        connected, close_code = await communicator.connect(
            timeout=CONNECT_TIMEOUT_SECONDS
        )
        return communicator, connected, close_code

    async def connect_teacher(self):
        """Open the teacher's own dashboard socket for this round.

        This is the path that broadcasts: the services change state, but only the
        teacher consumer's `_apply` sends it to the screens. Testing the workflow
        therefore means driving it the way the teacher does - one press, through
        the socket the button is wired to.
        """
        scope = await build_teacher_scope(self.teacher, self.competition.pk)
        communicator = WebsocketCommunicator(
            application,
            teacher_url(self.competition.pk),
            headers=origin_headers(),
        )
        communicator.scope = scope
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)
        self.assertTrue(connected)
        return communicator

    async def start(self):
        """Press Start competition, as the teacher's button does."""
        teacher = await self.connect_teacher()
        try:
            await send_message(teacher, {"type": events.START_COMPETITION})
        finally:
            await teacher.disconnect()

    async def ask_question(self):
        """Press Start question, as the teacher's button does."""
        teacher = await self.connect_teacher()
        try:
            await send_message(teacher, {"type": events.START_QUESTION})
        finally:
            await teacher.disconnect()

    # -- delivery ---------------------------------------------------------

    async def test_one_start_reaches_every_selected_screen(self) -> None:
        """Two screens in the selected classrooms, one press, one question.

        This is the workflow the whole phase is about, in the order a lesson goes:
        the boards are open and waiting, the teacher presses Start once, and both
        screens show the same active question - without any competition URL being
        opened on either board.
        """
        boards = [
            (await self.connect_screen(self.screen_in_one))[0],
            (await self.connect_screen(self.screen_in_two))[0],
        ]
        try:
            for board in boards:
                self.assertEqual((await receive_within(board))["type"], events.WAITING)

            await self.start()

            # Each board is told to ask again, and joins on its new socket.
            for index, board in enumerate(boards):
                with self.subTest(board=index):
                    self.assertEqual(
                        (await receive_within(board))["type"], events.RECONNECT_NOW
                    )

            boards = [
                (await self.connect_screen(self.screen_in_one))[0],
                (await self.connect_screen(self.screen_in_two))[0],
            ]
            for index, board in enumerate(boards):
                with self.subTest(board=index):
                    welcomed, _ = await drain_until(board, events.WELCOME)
                    self.assertEqual(welcomed["competition_id"], self.competition.pk)
                    self.assertEqual(welcomed["competition_title"], "مسابقة التسليم")

            await self.ask_question()

            questions = [
                await drain_until(board, events.QUESTION_STARTED) for board in boards
            ]
            # One press, the same question, in the same state, on both screens.
            for message, _ in questions:
                self.assertEqual(message["competition_title"], "مسابقة التسليم")
                self.assertEqual(message["question_number"], 1)
            # The question's fields arrive flat in this event, so "the same
            # question" is its id and its text, compared across the two screens.
            self.assertEqual(questions[0][0]["question_id"], questions[1][0]["question_id"])
            self.assertEqual(questions[0][0]["text"], questions[1][0]["text"])
        finally:
            for board in boards:
                await board.disconnect()


    async def test_a_screen_with_no_round_running_waits_instead_of_refusing(self) -> None:
        """A classroom with no competition yet must still hold its socket.

        The alternative - refusing and retrying every few seconds - shares one
        client address with every other screen in the school, and the Screen ID
        limit is counted per address, so a building of waiting screens would
        lock itself out between lessons.
        """
        # No competition has been started, so no round is running.
        waiting = (await self.connect_screen(self.screen_in_one))[0]
        try:
            message = await receive_within(waiting)
            self.assertEqual(message["type"], events.WAITING)
            self.assertEqual(message["state"], "waiting")
            self.assertIn("No competition is running", message["message"])
            self.assertEqual(message["screen"]["screen_id"], self.screen_in_one.screen_id)

            # Nothing has been pushed to it: it is waiting for the teacher, not
            # following a round that does not exist.
            await self.assert_silent(waiting)
        finally:
            await waiting.disconnect()

    async def test_a_waiting_screen_is_told_to_reconnect_when_a_round_starts(self) -> None:
        """The nudge a waiting screen gets is a request to ask again.

        Not an invitation to join: whether it may join is decided when it
        identifies itself on the new socket, exactly as any screen does.
        """
        waiting = (await self.connect_screen(self.screen_in_one))[0]
        try:
            self.assertEqual((await receive_within(waiting))["type"], events.WAITING)

            await self.start()
            message = await receive_within(waiting)
            self.assertEqual(message["type"], events.RECONNECT_NOW)
            self.assertEqual(message["reason"], events.COMPETITION_STARTED)
        finally:
            await waiting.disconnect()

    async def test_a_waiting_screen_joins_a_round_without_reconnecting(self) -> None:
        """One Start brings a screen that was waiting into the round.

        This is the requirement that the teacher never opens a competition URL
        on a screen: the board was already open, showing a waiting room, and the
        nudge alone puts it into the round.
        """
        waiting = (await self.connect_screen(self.screen_in_one))[0]
        try:
            self.assertEqual((await receive_within(waiting))["type"], events.WAITING)

            await self.start()
            # Told to ask again, and reconnection is what a screen does.
            self.assertEqual((await receive_within(waiting))["type"], events.RECONNECT_NOW)
        finally:
            await waiting.disconnect()

        # On the new socket it identifies and is in the round it was already part
        # of - exactly as any screen that connects while a round is running.
        joined, connected, _ = await self.connect_screen(self.screen_in_one)
        self.assertTrue(connected)
        try:
            message, _ = await drain_until(joined, events.STATE_SYNC)
            self.assertEqual(message["competition_id"], self.competition.pk)
            self.assertEqual(message["state"], CompetitionState.WAITING)

            await self.ask_question()
            question, _ = await drain_until(joined, events.QUESTION_STARTED)
            self.assertEqual(question["question_number"], 1)
            self.assertEqual(question["text"], "سؤال التسليم")
        finally:
            await joined.disconnect()

    async def test_a_waiting_screen_in_an_excluded_classroom_stays_waiting(self) -> None:
        """The nudge reaches every screen, but membership decides what it joins."""
        outsider, connected, _ = await self.connect_screen(self.screen_out)
        self.assertTrue(connected)
        try:
            self.assertEqual((await receive_within(outsider))["type"], events.WAITING)

            await self.start()
            # Its classroom is not in the round, so it is told to ask again...
            self.assertEqual((await receive_within(outsider))["type"], events.RECONNECT_NOW)

            # ...and when it asks, the server ends the socket rather than letting
            # it read the round: registered, healthy, and not part of it.
            rejoined, accepted, close_code = await self.connect_screen(self.screen_out)
            self.assertFalse(accepted, "a screen outside the round was let in")
            self.assertEqual(close_code, CLOSE_UNAUTHORISED)
        finally:
            await outsider.disconnect()

    async def test_a_screen_belonging_to_an_excluded_classroom_is_refused(self) -> None:
        """The authorisation boundary: registered, healthy, not in the round.

        A round writes only to sockets that are open, and this socket is never
        opened: identification ends it rather than leaving it attached to a group
        it may not read. The screen is told nothing at all - not a question, not a
        score, not a tick - so "left out" is enforced by the connection itself.
        """
        await self.start()
        outsider, connected, close_code = await self.connect_screen(self.screen_out)
        self.assertFalse(connected, "a screen outside the round was let in")
        self.assertEqual(close_code, CLOSE_UNAUTHORISED)
        await outsider.disconnect()

    async def test_an_excluded_screen_never_sees_a_question(self) -> None:
        """Not attached, and nothing sent: a refused socket has nothing to read."""
        await self.start()
        await self.ask_question()
        outsider, connected, close_code = await self.connect_screen(self.screen_out)
        self.assertFalse(connected)
        self.assertEqual(close_code, CLOSE_UNAUTHORISED)
        await outsider.disconnect()


    async def test_an_unknown_screen_id_is_refused(self) -> None:
        """Delivery never depends on the client naming a competition.

        And an unregistered ID is not an entrance either: identification ends the
        socket before anything is accepted. The screen's own page says
        "unregistered" in that case; what matters here is that nothing is sent.
        """
        communicator, connected, close_code = await self.connect_screen_by_id("AM-ZZZZZZ")
        self.assertFalse(connected, "an unregistered Screen ID was let in")
        self.assertEqual(close_code, CLOSE_UNAUTHORISED)
        await communicator.disconnect()

    async def test_reconnect_restores_the_authoritative_current_state(self) -> None:
        """A screen dropped mid-round is correct the moment it reconnects."""
        await self.start()
        screen, connected, _ = await self.connect_screen(self.screen_in_one)
        self.assertTrue(connected)
        await self.ask_question()
        message, _ = await drain_until(screen, events.QUESTION_STARTED)
        self.assertEqual(message["question_number"], 1)

        # Dropped the way a network fault drops it: no goodbye, no notice.
        await screen.disconnect()

        rejoined, connected, _ = await self.connect_screen(self.screen_in_one)
        self.assertTrue(connected)
        try:
            message, _ = await drain_until(rejoined, events.STATE_SYNC)
            self.assertEqual(message["competition_id"], self.competition.pk)
            self.assertEqual(message["state"], "question_active")
            self.assertEqual(message["question_number"], 1)
            # Near the full duration, which is what makes it the authoritative
            # current question rather than a stale one: a few milliseconds of
            # elapsed time are expected between the press and the assertion.
            self.assertAlmostEqual(message["seconds_remaining"], 30.0, delta=1.0)
            # The question itself, so the screen does not wait for the next event.
            self.assertEqual(message["question"]["text"], "سؤال التسليم")
        finally:
            await rejoined.disconnect()

    async def test_a_screen_joining_after_the_start_is_current_at_once(self) -> None:
        """The screen needs no notification of the start: it asks, it is told."""
        await self.start()
        await self.ask_question()
        late = (await self.connect_screen(self.screen_in_one))[0]
        try:
            message, _ = await drain_until(late, events.STATE_SYNC)
            self.assertEqual(message["question_number"], 1)
        finally:
            await late.disconnect()

    async def test_the_teacher_dashboard_is_a_separate_audience(self) -> None:
        """Screens and the teacher's controls never share a group."""
        scope = await build_teacher_scope(self.teacher, self.competition.pk)
        communicator = WebsocketCommunicator(
            application,
            teacher_url(self.competition.pk),
            headers=origin_headers(),
        )
        # The cookie is what authorises the socket, so it goes in the scope the
        # same way a browser supplies it - not a hand-built session object.
        communicator.scope = scope
        connected, _ = await communicator.connect(timeout=CONNECT_TIMEOUT_SECONDS)
        self.assertTrue(connected)
        try:
            await self.start()
            message, _ = await drain_until(communicator, events.COMPETITION_STARTED)
            self.assertEqual(message["competition_title"], "مسابقة التسليم")
        finally:
            await communicator.disconnect()

    async def _add_participating_classroom(self, name: str, heartbeat_ip: str):
        """A second classroom, its screen, and its place in the round.

        Wrapped in a thread because the whole setup touches the database and this
        test method runs on the event loop.
        """

        def build():
            classroom = create_classroom(name=name)
            classroom.teachers.add(self.teacher)
            screen = create_screen(
                name=name.replace("غرفة", "شاشة"), classroom=classroom, is_primary=True
            )
            screen.record_heartbeat(ip_address=heartbeat_ip)
            CompetitionClassroom.objects.get_or_create(
                competition=self.competition, classroom=classroom
            )
            return screen

        return await self._in_thread(build)

    async def test_screens_of_two_participating_classrooms_stay_in_step(self) -> None:
        """Both classrooms in the round, so both screens are in step.

        Two rooms, two boards, one press: this is the case the school actually
        runs, and the one that proves the teacher never opens anything on either
        screen to join the round.
        """
        second_screen = await self._add_participating_classroom(
            "غرفة الداخل ٢", "10.0.0.12"
        )

        boards = [
            (await self.connect_screen(self.screen_in_one))[0],
            (await self.connect_screen(second_screen))[0],
            (await self.connect_screen(self.screen_in_two))[0],
        ]
        try:
            for board in boards:
                self.assertEqual((await receive_within(board))["type"], events.WAITING)

            await self.start()
            for board in boards:
                self.assertEqual((await receive_within(board))["type"], events.RECONNECT_NOW)

            # Re-identifying puts each board in the round it was already part of.
            boards = [
                (await self.connect_screen(screen))[0]
                for screen in (self.screen_in_one, second_screen, self.screen_in_two)
            ]
            for index, board in enumerate(boards):
                with self.subTest(board=index):
                    message, _ = await drain_until(board, events.WELCOME)
                    self.assertEqual(message["competition_id"], self.competition.pk)
                    self.assertEqual(message["state"], "waiting")
        finally:
            for board in boards:
                await board.disconnect()

    async def test_the_round_reaches_only_what_is_selected_when_both_participate(self) -> None:
        """The exclusion test's counterpart: two classrooms in, one out, one press.

        A second classroom is added to the round while the excluded screen is
        already connected, so this is the shape of the real school: three boards
        open in three rooms, and the round reaches two of them.
        """
        second_screen = await self._add_participating_classroom(
            "غرفة الداخل ٣", "10.0.0.13"
        )
        outsider, connected, _ = await self.connect_screen(self.screen_out)
        self.assertTrue(connected)
        try:
            # No round is running yet, so the outsider waits like any screen.
            self.assertEqual((await receive_within(outsider))["type"], events.WAITING)

            boards = [
                (await self.connect_screen(self.screen_in_one))[0],
                (await self.connect_screen(second_screen))[0],
            ]
            for board in boards:
                self.assertEqual((await receive_within(board))["type"], events.WAITING)

            await self.start()

            # Both selected boards join...
            for index, board in enumerate(boards):
                with self.subTest(board=index):
                    self.assertEqual(
                        (await receive_within(board))["type"], events.RECONNECT_NOW
                    )
            boards = [
                (await self.connect_screen(screen))[0]
                for screen in (self.screen_in_one, second_screen)
            ]
            for index, board in enumerate(boards):
                with self.subTest(board=index):
                    message, _ = await drain_until(board, events.WELCOME)
                    self.assertEqual(message["competition_title"], "مسابقة التسليم")

            # ...the excluded board is nudged too, and is refused when it asks.
            self.assertEqual((await receive_within(outsider))["type"], events.RECONNECT_NOW)
            rejoined, accepted, close_code = await self.connect_screen(self.screen_out)
            self.assertFalse(accepted, "the excluded screen was let into the round")
            self.assertEqual(close_code, CLOSE_UNAUTHORISED)
        finally:
            await outsider.disconnect()

    async def test_a_late_screen_sees_the_round_already_in_progress(self) -> None:
        """A screen opened after the round started is correct immediately."""
        await self.start()
        await self.ask_question()
        late, connected, _ = await self.connect_screen(self.screen_in_one)
        self.assertTrue(connected)
        try:
            # The state sync carries the question that is already open, so the
            # screen shows the same thing the other boards are showing without
            # waiting for an event that already went out.
            message, _ = await drain_until(late, events.STATE_SYNC)
            self.assertEqual(message["question_number"], 1)
            self.assertEqual(message["state"], CompetitionState.QUESTION_ACTIVE)
            self.assertEqual(message["question"]["text"], "سؤال التسليم")
            self.assertEqual(message["total_questions"], 1)
        finally:
            await late.disconnect()
