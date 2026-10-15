"""Focused tests for the fixed teacher-facing surfaces.

These assert against the *real routes the teacher uses in production*, not
against a separate template that happens to render. Where the fix was that the
builder's seed was empty, the test asserts the seeded state rather than the
server-side DOM: the page ships empty containers that JavaScript fills, so
"the option input is present in the HTML" would prove nothing at all.

Covered here:

* The teacher's Create Question route serves the simplified builder, with none
  of the legacy Django Admin headings (Presentation / Type settings / Type
  config / JSON / Availability).
* The builder seeds its state from the question being edited, so editing does
  not silently drop the answer key.
* The preview never reveals the correct answer or the explanation.
* The Start Competition button is a visible primary action on the list and the
  detail page, above the screens table.
* A prepared competition is not started by saving, by planning, or by a GET.
"""

from __future__ import annotations

import json
import re

from django.test import TestCase
from django.urls import reverse

from competitions.models import Competition, CompetitionState
from core.tests.factories import (
    create_classroom,
    create_competition,
    create_question,
    create_subject,
    create_teacher,
    create_true_false_question,
)
from questions.models import QuestionType

TITLE = "UI9 Acceptance Competition"

# Labels that belong to the Django Admin form, not the teacher's builder. If any
# of these reach the teacher's page, the teacher is being shown the wrong form.
LEGACY_ADMIN_HEADINGS = (
    "Presentation",
    "Type settings",
    "Availability",
)
# The Admin's fieldset explains its own JSON fields in exactly these words.
LEGACY_JSON_WORDING = (
    "JSON describing the question",
    "Ordering: {",
    "Classification: {",
    "Short answer: {",
)


class TeacherQuestionRouteTests(TestCase):
    """The route a teacher actually uses to create a question."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui9.accept.teacher")
        cls.subject = create_subject(name="UI9 Acceptance Subject", slug="ui9-accept")

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def the_route_the_teacher_uses(self):
        return reverse("teacher:create_question")

    def test_the_teacher_create_route_serves_the_simplified_builder(self) -> None:
        response = self.client.get(self.the_route_the_teacher_uses())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "teacher/question_form.html")

        # The builder's own sections are present...
        for marker in (
            "Choose a question type",
            "Question text",
            "Live preview",
            "Answer time",
        ):
            with self.subTest(marker=marker):
                self.assertContains(response, marker)

    def test_no_legacy_admin_headings_reach_the_teacher(self) -> None:
        response = self.client.get(self.the_route_the_teacher_uses())
        content = response.content.decode()
        for heading in LEGACY_ADMIN_HEADINGS:
            with self.subTest(heading=heading):
                self.assertNotContains(response, heading)
        for wording in LEGACY_JSON_WORDING:
            with self.subTest(wording=wording):
                self.assertNotContains(response, wording)

    def test_no_teacher_facing_text_input_requests_json(self) -> None:
        """No control asks a teacher to type the model's JSON columns.

        The columns still exist as hidden inputs - that is the storage format the
        scoring engine uses - but none is rendered as an editable control, and no
        label points a teacher at one.
        """
        response = self.client.get(self.the_route_the_teacher_uses())
        internal_fields = ("options", "type_config", "accepted_answers", "correct_order")
        for name in internal_fields:
            with self.subTest(field=name):
                self.assertNotRegex(
                    response.content.decode(),
                    rf'<label[^>]*for="id_{name}"',
                )
        # ...and every JSON column that is present really is hidden.
        for name in internal_fields:
            for element in re.findall(
                rf'<input[^>]*name="{name}"[^>]*>', response.content.decode()
            ):
                self.assertIn('type="hidden"', element)

    def test_all_five_question_types_are_offered(self) -> None:
        from questions.models import QuestionType

        response = self.client.get(self.the_route_the_teacher_uses())
        for value, _label in QuestionType.choices:
            with self.subTest(question_type=value):
                self.assertContains(response, f'value="{value}"')

    # -- the seed ---------------------------------------------------------

    def test_the_builder_is_seeded_from_the_question_being_edited(self) -> None:
        """Editing must not fall back to placeholder options.

        This is the defect the phase turned on: the view filled `preview_data`
        but the template renders `builder_data`, so the script received an empty
        string and the builder rebuilt every question from "Option A" / "Item 1"
        placeholders - saving an edit then wrote those placeholders over the
        real answer key.
        """
        question = create_question(
            text="ما حاصل ضرب ٧ في ٨؟",
            subject=self.subject,
            options=["١٤", "٤٨", "٥٦"],
            correct_option="٤٨",
        )
        response = self.client.get(
            reverse("teacher:edit_question", kwargs={"pk": question.pk})
        )
        self.assertEqual(response.status_code, 200)

        seeded = response.context["builder_data"]
        self.assertEqual(seeded["question_type"], question.question_type)
        self.assertEqual(seeded["options"], question.options)
        self.assertEqual(seeded["correct_option"], question.correct_option)
        self.assertEqual(seeded["text"], question.text)
        self.assertEqual(seeded["subject_id"], question.subject_id)

    def test_a_new_question_is_seeded_with_sensible_defaults(self) -> None:
        response = self.client.get(self.the_route_the_teacher_uses())
        seeded = response.context["builder_data"]
        self.assertEqual(seeded["question_type"], QuestionType.MULTIPLE_CHOICE)
        self.assertEqual(seeded["options"], [])
        self.assertIsNone(seeded["correct_answer"])

    def test_editing_true_false_selects_the_stored_answer_control(self) -> None:
        """The browser reads these radios when it first syncs the hidden key."""
        for answer in (True, False):
            with self.subTest(answer=answer):
                question = create_true_false_question(
                    subject=self.subject, value=answer
                )
                response = self.client.get(
                    reverse("teacher:edit_question", kwargs={"pk": question.pk})
                )
                radios = re.findall(
                    r'<input[^>]*name="builder-correct-answer"[^>]*>',
                    response.content.decode(),
                )
                checked = [radio for radio in radios if re.search(r'\bchecked\b', radio)]
                self.assertEqual(len(checked), 1)
                self.assertIn(f'value="{str(answer).lower()}"', checked[0])

    def test_the_seeded_state_reaches_the_page_as_json(self) -> None:
        question = create_question(text="عنصر واحد", subject=self.subject)
        response = self.client.get(
            reverse("teacher:edit_question", kwargs={"pk": question.pk})
        )
        match = re.search(
            r'<script id="question-builder-data"[^>]*>(.*?)</script>',
            response.content.decode(),
            re.DOTALL,
        )
        self.assertIsNotNone(match, "the seed script tag is missing")
        parsed = json.loads(match.group(1))
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["text"], "عنصر واحد")

    # -- the preview ------------------------------------------------------

    def test_the_preview_hides_the_correct_answer(self) -> None:
        """A preview that shows the answer can leak it onto a shared display."""
        question = create_question(
            text="أي كوكب أقرب إلى الشمس؟",
            subject=self.subject,
            options=["الزهرة", "عطارد", "المريخ"],
            correct_option="عطارد",
            explanation="عطارد أقرب الكواكب إلى الشمس.",
        )
        response = self.client.get(
            reverse("teacher:edit_question", kwargs={"pk": question.pk})
        )
        content = response.content.decode()

        # No option is marked, and the explanation is not previewed.
        self.assertNotIn("is-correct", content)
        self.assertNotIn("Accepted answers:", content)
        preview = re.search(r'<div id="question-preview">([\s\S]*?)</div>', content)
        self.assertIsNotNone(preview)
        preview_body = preview.group(1)
        self.assertIn("عطارد", preview_body)  # the option is listed
        self.assertNotIn("الإجابة الصحيحة", preview_body)

    def test_the_preview_shows_the_hint_but_not_the_explanation(self) -> None:
        """A hint is for the room; the explanation is only revealed afterwards."""
        question = create_question(
            text="سؤال ملاحظة",
            subject=self.subject,
            hint="تلميح للمعلم، يظهر للطلاب أيضًا.",
            explanation="هذا الشرح يأتي بعد انتهاء وقت الإجابة.",
        )
        response = self.client.get(
            reverse("teacher:edit_question", kwargs={"pk": question.pk})
        )
        content = response.content.decode()
        preview = re.search(r'<div id="question-preview">([\s\S]*?)</div>', content).group(1)
        self.assertIn("تلميح للمعلم", preview)
        self.assertNotIn("هذا الشرح يأتي بعد انتهاء وقت الإجابة", preview)


class StartCompetitionVisibilityTests(TestCase):
    """The Start Competition action must be impossible to miss."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui9.start.teacher")
        cls.subject = create_subject(name="UI9 Start Subject", slug="ui9-start")
        cls.room = create_classroom(name="UI9 Start Room")
        cls.room.teachers.add(cls.teacher)

    def setUp(self) -> None:
        self.competition = create_competition(
            teacher=self.teacher,
            title=TITLE,
            classrooms=[self.room],
            questions=[
                create_question(text="سؤال البداية", subject=self.subject),
                create_question(text="سؤال البداية ٢", subject=self.subject),
            ],
        )
        self.client.force_login(self.teacher)

    def detail(self):
        return self.client.get(
            reverse("teacher:competition_detail", args=[self.competition.pk])
        )

    def test_the_start_button_is_on_the_detail_page(self) -> None:
        response = self.detail()
        self.assertContains(response, "Start Competition")
        self.assertContains(response, reverse("teacher:start_competition", args=[self.competition.pk]))

    def test_the_start_button_is_a_primary_action(self) -> None:
        response = self.detail()
        self.assertContains(response, 'class="button button--primary"')

    def test_the_start_button_precedes_the_screens_table(self) -> None:
        """It must not be buried under the statistics and screen listing."""
        response = self.detail()
        content = response.content.decode()
        start_page = content.find("start-heading")  # the start panel's heading
        screens_table = content.find("Screens this competition will reach")
        self.assertNotEqual(start_page, -1)
        self.assertNotEqual(screens_table, -1)
        self.assertLess(start_page, screens_table)

    def test_the_start_button_is_on_the_list_page(self) -> None:
        response = self.client.get(reverse("teacher:competitions"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, reverse("teacher:start_competition", args=[self.competition.pk])
        )
        self.assertContains(response, "Start Competition")

    def test_the_start_button_is_here_in_arabic(self) -> None:
        self.client.post(
            reverse("teacher:set_language"), {"language": "ar", "next": "/teacher/"}
        )
        response = self.detail()
        self.assertContains(response, "بدء المسابقة")

    def test_starting_changes_the_real_state_in_the_database(self) -> None:
        response = self.client.post(
            reverse("teacher:start_competition", args=[self.competition.pk])
        )
        self.assertEqual(response.status_code, 302)

        self.competition.refresh_from_db()
        self.assertTrue(self.competition.is_started)
        self.assertTrue(self.competition.is_live)
        self.assertIsNotNone(self.competition.started_at)
        self.assertEqual(
            Competition.objects.get(pk=self.competition.pk).state,
            CompetitionState.WAITING,
        )

    def test_a_get_never_starts_the_competition(self) -> None:
        response = self.client.get(
            reverse("teacher:start_competition", args=[self.competition.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.competition.refresh_from_db()
        self.assertFalse(self.competition.is_started, "a GET started the round")

    def test_saving_a_draft_never_starts_it(self) -> None:
        session = self.client.session
        from teacher.wizard import SESSION_KEY

        session[SESSION_KEY] = {
            "step": "review",
            "subject_id": self.subject.pk,
            "basic": {"title": "UI9 Draft", "subject": "", "grade": "", "description": ""},
            "classroom_ids": [str(self.room.pk)],
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
            },
            "question_ids": [str(self.competition.questions.first().pk)],
        }
        session.save()
        self.client.post(
            reverse("teacher:create_competition"), {"action": "save_draft"}
        )
        self.competition.refresh_from_db()
        self.assertFalse(self.competition.is_started)

    def test_planning_a_time_never_starts_it(self) -> None:
        response = self.client.post(
            reverse("teacher:plan_competition", args=[self.competition.pk]),
            {"planned_for": "2030-01-01 10:30"},
        )
        self.assertRedirects(
            response, reverse("teacher:competition_detail", args=[self.competition.pk])
        )
        self.competition.refresh_from_db()
        self.assertIsNotNone(self.competition.planned_for)
        self.assertFalse(self.competition.is_started)
        self.assertEqual(self.competition.lifecycle_label, "planned")

    def test_a_second_start_is_refused(self) -> None:
        from live.services import start_competition

        start_competition(self.competition)
        response = self.client.post(
            reverse("teacher:start_competition", args=[self.competition.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.competition.refresh_from_db()
        self.assertTrue(self.competition.is_started)

    def test_a_competition_without_questions_cannot_start(self) -> None:
        self.competition.questions.all().delete()
        response = self.client.post(
            reverse("teacher:start_competition", args=[self.competition.pk])
        )
        self.assertContains(
            response, "There are no questions in this competition"
        )
        self.competition.refresh_from_db()
        self.assertFalse(self.competition.is_started)

    def test_the_detail_page_carries_the_start_form_context(self) -> None:
        """The offline-screen warning must appear on the detail page, not just
        on the start form.

        The Start Competition button moved onto the detail page, but the detail
        view kept building its own context with `targeting` alone. The start form
        reads `has_offline_screens`, which was therefore never set, so the warning
        and its acknowledgement checkbox silently did not render - on the very
        page where a teacher decides to start a round with screens offline.
        """
        from core.tests.factories import create_screen

        screen = create_screen(name="UI9 Offline Board", classroom=self.room)
        # Registered but never heard from: exactly the case the warning names.
        screen.last_seen = None
        screen.save(update_fields=["last_seen"])

        response = self.detail()
        self.assertContains(
            response, "Some expected screens are offline"
        )
        self.assertContains(response, 'name="acknowledge_screens"')

    def test_a_fully_connected_competition_says_so(self) -> None:
        from django.utils import timezone

        from core.tests.factories import create_screen

        screen = create_screen(name="UI9 Live Board", classroom=self.room)
        screen.record_heartbeat(ip_address="10.9.9.9")
        response = self.detail()
        self.assertContains(
            response, "Every classroom in this competition has a screen connected."
        )

    def test_a_competition_without_any_screen_blocker_listed(self) -> None:
        self.competition.classrooms.all().delete()
        response = self.client.post(
            reverse("teacher:start_competition", args=[self.competition.pk])
        )
        self.assertContains(response, "There are no classrooms taking part.")
        self.competition.refresh_from_db()
        self.assertFalse(self.competition.is_started)

    def test_another_teacher_cannot_start_this_competition(self) -> None:
        stranger = create_teacher(username="ui9.stranger.teacher")
        self.client.force_login(stranger)
        response = self.client.post(
            reverse("teacher:start_competition", args=[self.competition.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.competition.refresh_from_db()
        self.assertFalse(self.competition.is_started)
