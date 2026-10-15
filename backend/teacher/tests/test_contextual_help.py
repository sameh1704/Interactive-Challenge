from __future__ import annotations

from django.test import TestCase
from django.conf import settings
from django.urls import reverse

from classrooms.models import Classroom
from core.tests.factories import create_subject
from core.tests.utils import create_teacher
from teacher.tournament_wizard import (
    SESSION_KEY as TOURNAMENT_SESSION_KEY,
    STEP_ADVANCEMENT,
)
from teacher.wizard import (
    SESSION_KEY as COMPETITION_SESSION_KEY,
    STEP_SUBJECT_CLASSROOMS,
    STEP_QUESTIONS,
    STEP_SETTINGS,
)


class ContextualHelpTests(TestCase):
    def setUp(self):
        self.teacher = create_teacher(username="ui5.context.teacher")
        self.subject = create_subject(name="Science", slug="ui5-science")
        self.classrooms = []
        for number in range(1, 3):
            classroom = Classroom.objects.create(
                name=f"UI5 Help Classroom {number}",
                grade="Grade 7",
                active=True,
            )
            classroom.teachers.add(self.teacher)
            self.classrooms.append(classroom)
        self.client.force_login(self.teacher)

    def save_session(self, key, value):
        session = self.client.session
        session[key] = value
        session.save()

    def test_major_teacher_pages_render_contextual_help(self):
        urls = (
            reverse("teacher:dashboard"),
            reverse("teacher:competitions"),
            reverse("teacher:questions"),
            reverse("teacher:tournaments"),
            reverse("teacher:reports"),
            reverse("teacher:hall_of_fame"),
            reverse("teacher:help"),
            reverse("teacher:create_competition"),
            reverse("teacher:create_question"),
            reverse("teacher:create_tournament"),
        )
        empty_state_urls = {
            reverse("teacher:competitions"),
            reverse("teacher:questions"),
            reverse("teacher:tournaments"),
            reverse("teacher:hall_of_fame"),
        }
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "contextual-help.")
                if url in empty_state_urls:
                    self.assertContains(response, 'class="panel empty-state"')

    def test_competition_class_scoring_and_question_hints_are_rendered(self):
        steps = (
            (
                STEP_SUBJECT_CLASSROOMS,
                "الموضوع الذي تختاره هنا هو الموضوع الوحيد الذي تُعرض أسئلته",
            ),
            (
                STEP_SETTINGS,
                "يتم احتساب النتيجة على السيرفر، ويمكن إضافة نقاط للسرعة.",
            ),
            (
                STEP_QUESTIONS,
                "Only questions from the subject you chose are offered here",
            ),
        )
        for step, hint in steps:
            with self.subTest(step=step):
                self.save_session(
                    COMPETITION_SESSION_KEY,
                    {
                        "step": step,
                        "basic": {
                            "title": "UI5 Help competition",
                            "subject": "Science",
                            "grade": "Grade 7",
                            "description": "",
                        },
                        # A question bank is organised by subject, so the wizard
                        # carries one from step 2 onwards.
                        "subject_id": str(self.subject.pk),
                        "classroom_ids": [
                            str(classroom.pk) for classroom in self.classrooms
                        ],
                        "settings": {
                            "duration_seconds": 30,
                            "scoring_mode": "standard",
                            "speed_bonus": 10,
                        },
                        "question_ids": [],
                    },
                )
                response = self.client.get(reverse("teacher:create_competition"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, hint)

    def test_tournament_advancement_hint_and_first_use_notice_are_rendered(self):
        self.save_session(
            TOURNAMENT_SESSION_KEY,
            {
                "step": STEP_ADVANCEMENT,
                "tournament": {
                    "name": "UI5 Help tournament",
                    "subject": "Science",
                    "grade": "Grade 7",
                    "season": "2026",
                    "description": "",
                },
                "classroom_ids": [str(classroom.pk) for classroom in self.classrooms],
                "stage_template": "qualification_final",
                "advancing_count": "1",
                "semi_advancing_count": "1",
                "competition_ids": {
                    "qualification": [],
                    "semi_final": [],
                    "final": [],
                },
            },
        )
        response = self.client.get(reverse("teacher:create_tournament"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "حدد عدد الفصول التي تنتقل إلى المرحلة التالية.")
        self.assertContains(response, 'data-first-use="tournament-wizard"')
        self.assertContains(response, "Don't show again")

    def test_question_type_help_explains_ordering_in_both_languages(self):
        response = self.client.get(reverse("teacher:create_question"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Which question type should I choose?")
        self.assertContains(
            response, "الترتيب: يستخدم عندما يكون ترتيب العناصر مهمًا."
        )
        self.assertContains(response, 'lang="ar"')
        self.assertContains(response, 'dir="rtl"')

    def test_language_switch_changes_locale_and_preserves_tournament_wizard_session(self):
        wizard_state = {
            "step": "tournament",
            "tournament": {
                "name": "UI6 retained tournament name",
                "subject": "Science",
                "grade": "Grade 7",
                "season": "2026",
                "description": "",
            },
            "classroom_ids": [],
            "stage_template": "qualification_final",
            "advancing_count": "2",
            "semi_advancing_count": "2",
            "competition_ids": {
                "qualification": [],
                "semi_final": [],
                "final": [],
            },
        }
        self.save_session(TOURNAMENT_SESSION_KEY, wizard_state)
        switch_url = reverse("set_language")
        wizard_url = reverse("teacher:create_tournament")

        response = self.client.post(
            switch_url,
            {"language": "ar", "next": wizard_url},
        )
        self.assertRedirects(response, wizard_url, fetch_redirect_response=False)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "ar")
        self.assertEqual(self.client.session[TOURNAMENT_SESSION_KEY], wizard_state)

        arabic_response = self.client.get(wizard_url)
        self.assertEqual(arabic_response.status_code, 200)
        # Plain markup, not `html=True`: that mode compares whole elements, and a
        # real page's `<html>` element has a `<head>` and a `<body>` under it, so
        # a needle holding nothing but the tag itself can never match it.
        # Attribute-by-attribute rather than the whole tag: the element also
        # carries the theme attribute (UI-7), and matching the tag exactly would
        # read that as the language having moved.
        self.assertContains(arabic_response, 'lang="ar"')
        self.assertContains(arabic_response, 'dir="rtl"')
        self.assertContains(arabic_response, "إعداد البطولة")
        self.assertEqual(self.client.session[TOURNAMENT_SESSION_KEY], wizard_state)

        response = self.client.post(
            switch_url,
            {"language": "en", "next": wizard_url},
        )
        self.assertRedirects(response, wizard_url, fetch_redirect_response=False)
        english_response = self.client.get(wizard_url)
        self.assertContains(english_response, 'lang="en"')
        self.assertContains(english_response, 'dir="ltr"')
        self.assertContains(english_response, "UI6 retained tournament name")
        self.assertEqual(self.client.session[TOURNAMENT_SESSION_KEY], wizard_state)
