"""Focused tests for the teacher-facing Question Builder."""

from __future__ import annotations

import io
import json
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from core.tests.factories import create_subject
from core.tests.utils import create_administrator, create_question, create_teacher
from questions.models import Question, QuestionType


class QuestionBuilderTests(TestCase):
    def setUp(self) -> None:
        self.teacher = create_teacher(username="ui3.builder.teacher")
        self.other_teacher = create_teacher(username="ui3.builder.other")
        self.admin = create_administrator(username="ui3.builder.admin")
        # A question belongs to a subject - it is what the bank is organised by -
        # so every payload needs one. Supplied here rather than in each payload so
        # a test that wants to check the subject's validation can still override it.
        self.subject = create_subject(name="Mathematics", slug="ui3-maths")

        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media.cleanup)

    def login(self, user=None) -> None:
        self.client.force_login(user or self.teacher)

    @staticmethod
    def multiple_choice_data(**overrides):
        data = {
            "text": "What is 2 + 2?",
            "question_type": QuestionType.MULTIPLE_CHOICE,
            "options": json.dumps(["3", "4", "5"]),
            "correct_option": "4",
            "correct_answer": "",
            "type_config": "{}",
            "hint": "Add the two numbers.",
            "explanation": "Two plus two is four.",
            "duration_seconds": "30",
            "active": "on",
        }
        data.update(overrides)
        return data

    def post_create(self, data, *, follow=False, files=None):
        if files:
            data = {**data, **files}
        data = {**data}
        data.setdefault("subject", self.subject.pk)
        return self.client.post(
            reverse("teacher:create_question"),
            data=data,
            follow=follow,
        )

    def post_edit(self, question, data, *, follow=False):
        data = {**data}
        data.setdefault("subject", self.subject.pk)
        return self.client.post(
            reverse("teacher:edit_question", kwargs={"pk": question.pk}),
            data=data,
            follow=follow,
        )

    def test_question_builder_requires_staff_login(self):
        response = self.client.get(reverse("teacher:create_question"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_teacher_can_open_builder_and_question_bank(self):
        self.login()
        for url in (
            reverse("teacher:create_question"),
            reverse("teacher:questions"),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_all_supported_question_types_can_be_created(self):
        self.login()
        examples = (
            (
                QuestionType.MULTIPLE_CHOICE,
                self.multiple_choice_data(),
            ),
            (
                QuestionType.TRUE_FALSE,
                self.multiple_choice_data(
                    question_type=QuestionType.TRUE_FALSE,
                    options="[]",
                    correct_option="",
                    correct_answer="false",
                    type_config="{}",
                ),
            ),
            (
                QuestionType.ORDERING,
                self.multiple_choice_data(
                    question_type=QuestionType.ORDERING,
                    options="[]",
                    correct_option="",
                    type_config=json.dumps(
                        {
                            "items": ["first", "second", "third"],
                            "correct_order": ["second", "first", "third"],
                        }
                    ),
                ),
            ),
            (
                QuestionType.CLASSIFICATION,
                self.multiple_choice_data(
                    question_type=QuestionType.CLASSIFICATION,
                    options="[]",
                    correct_option="",
                    type_config=json.dumps(
                        {
                            "categories": ["Mammal", "Bird"],
                            "assignments": [
                                {"label": "Dog", "category": "Mammal"},
                                {"label": "Eagle", "category": "Bird"},
                            ],
                        }
                    ),
                ),
            ),
            (
                QuestionType.SHORT_ANSWER,
                self.multiple_choice_data(
                    question_type=QuestionType.SHORT_ANSWER,
                    options="[]",
                    correct_option="",
                    type_config=json.dumps(
                        {
                            "accepted_answers": ["4", "four"],
                            "case_sensitive": False,
                        }
                    ),
                ),
            ),
        )
        for q_type, data in examples:
            with self.subTest(question_type=q_type):
                response = self.post_create(data)
                self.assertEqual(response.status_code, 302)
                question = Question.objects.get(question_type=q_type)
                self.assertEqual(question.text, "What is 2 + 2?")
                self.assertEqual(question.hint, "Add the two numbers.")
                self.assertEqual(question.explanation, "Two plus two is four.")

    def test_each_type_rejects_an_invalid_answer_key(self):
        self.login()
        invalid_examples = (
            self.multiple_choice_data(
                options=json.dumps(["one"]),
                correct_option="one",
            ),
            self.multiple_choice_data(
                question_type=QuestionType.TRUE_FALSE,
                options="[]",
                correct_option="",
                correct_answer="",
            ),
            self.multiple_choice_data(
                question_type=QuestionType.ORDERING,
                options="[]",
                correct_option="",
                type_config=json.dumps(
                    {"items": ["only one"], "correct_order": ["only one"]}
                ),
            ),
            self.multiple_choice_data(
                question_type=QuestionType.CLASSIFICATION,
                options="[]",
                correct_option="",
                type_config=json.dumps(
                    {
                        "categories": ["Only one"],
                        "assignments": [{"label": "X", "category": "Only one"}],
                    }
                ),
            ),
            self.multiple_choice_data(
                question_type=QuestionType.SHORT_ANSWER,
                options="[]",
                correct_option="",
                type_config=json.dumps({"accepted_answers": []}),
            ),
        )
        for data in invalid_examples:
            with self.subTest(question_type=data["question_type"]):
                response = self.post_create(data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertEqual(Question.objects.count(), 0)

    def test_bad_json_shape_is_reported_instead_of_raising(self):
        self.login()
        response = self.post_create(
            self.multiple_choice_data(options=json.dumps({"not": "a list"}))
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("options", response.context["form"].errors)

        response = self.post_create(
            self.multiple_choice_data(type_config=json.dumps(["not", "an object"]))
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("type_config", response.context["form"].errors)

    def test_edit_persists_type_fields_hint_explanation_and_duration(self):
        self.login()
        question = create_question(text="Original question")
        response = self.client.get(
            reverse("teacher:edit_question", kwargs={"pk": question.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Original question")

        response = self.post_edit(
            question,
            self.multiple_choice_data(
                text="Updated question",
                hint="Updated hint",
                explanation="Updated explanation",
                duration_seconds="45",
                options=json.dumps(["4", "5"]),
                correct_option="5",
            ),
        )
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.text, "Updated question")
        self.assertEqual(question.options, ["4", "5"])
        self.assertEqual(question.correct_option, "5")
        self.assertEqual(question.hint, "Updated hint")
        self.assertEqual(question.explanation, "Updated explanation")
        self.assertEqual(question.duration_seconds, 45)

    def test_edit_preserves_existing_image_when_no_replacement_is_uploaded(self):
        self.login()
        image_bytes = io.BytesIO()
        Image.new("RGB", (1, 1), color="white").save(image_bytes, format="PNG")
        question = create_question()
        question.image.save(
            "ui3-existing.png",
            SimpleUploadedFile("ui3-existing.png", image_bytes.getvalue()),
        )
        saved_image_name = question.image.name

        response = self.post_edit(question, self.multiple_choice_data())

        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.image.name, saved_image_name)

    def test_question_image_upload_is_optional_and_validated(self):
        self.login()
        image_bytes = io.BytesIO()
        Image.new("RGB", (1, 1), color="white").save(image_bytes, format="PNG")
        image = SimpleUploadedFile(
            "ui3-question.png", image_bytes.getvalue(), content_type="image/png"
        )
        response = self.post_create(self.multiple_choice_data(), files={"image": image})
        self.assertEqual(response.status_code, 302)
        question = Question.objects.get()
        self.assertTrue(question.image.name.endswith(".png"))

        invalid = SimpleUploadedFile(
            "ui3-question.exe", b"not an image", content_type="application/octet-stream"
        )
        response = self.post_create(
            self.multiple_choice_data(text="Invalid image"),
            files={"image": invalid},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("image", response.context["form"].errors)

    def test_preview_is_rendered_and_seeded_with_current_question_data(self):
        """The builder must seed from the stored question, not placeholder rows.

        A builder that renders empty state cannot be verified from the server
        HTML alone: the server renders empty containers that the script fills.
        What *is* visible server-side is the state the script seeds from, so this
        asserts that state - which is what decides whether editing a question
        keeps or throws away its answer key.
        """
        self.login()
        question = create_question(text="Preview this", hint="A teacher hint.")
        question.hint = "A teacher hint."
        question.save(update_fields=["hint"])
        response = self.client.get(
            reverse("teacher:edit_question", kwargs={"pk": question.pk})
        )
        self.assertEqual(response.status_code, 200)

        seeded = response.context["builder_data"]
        self.assertEqual(seeded["question_type"], question.question_type)
        self.assertEqual(seeded["text"], question.text)
        self.assertEqual(seeded["options"], question.options)
        self.assertEqual(seeded["correct_option"], question.correct_option)
        self.assertEqual(seeded["hint"], "A teacher hint.")

        # The seed must reach the page as JSON, not as an empty string.
        self.assertContains(response, 'id="question-builder-data"')
        state = response.context["builder_data"]
        self.assertIn(question.correct_option, state["options"])
        self.assertContains(response, "Preview this")
        self.assertContains(response, "A teacher hint.")

    def test_invalid_submission_keeps_values_and_shows_validation(self):
        self.login()
        response = self.post_create(
            self.multiple_choice_data(
                text="Keep my text",
                options=json.dumps(["single option"]),
                correct_option="single option",
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Keep my text")
        self.assertContains(response, "between 2 and 6 options")

    def test_save_actions_redirect_to_their_destinations(self):
        self.login()
        response = self.post_create(
            self.multiple_choice_data(save_option="save_another")
        )
        self.assertEqual(response.url, reverse("teacher:create_question"))

        response = self.post_create(
            self.multiple_choice_data(
                text="Competition-bound question",
                save_option="save_to_competition",
            )
        )
        self.assertIn(reverse("teacher:create_competition"), response.url)
        self.assertIn("question_id=", response.url)

    def test_shared_question_bank_can_be_edited_by_another_teacher(self):
        question = create_question(text="Shared question")
        self.login(self.other_teacher)
        response = self.client.get(
            reverse("teacher:edit_question", kwargs={"pk": question.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_administrator_can_edit_any_question(self):
        question = create_question(text="Administrator question")
        self.login(self.admin)
        response = self.client.get(
            reverse("teacher:edit_question", kwargs={"pk": question.pk})
        )
        self.assertEqual(response.status_code, 200)


class TeacherWorkspaceURLTests(TestCase):
    def test_teacher_and_workspace_namespaces_both_resolve(self):
        self.assertEqual(reverse("teacher:dashboard"), "/teacher/")
        self.assertEqual(reverse("workspace:dashboard"), "/workspace/")

    def test_workspace_alias_still_serves_the_teacher_workspace(self):
        teacher = create_teacher(username="ui3.workspace.alias")
        self.client.force_login(teacher)
        response = self.client.get(reverse("workspace:dashboard"))
        self.assertEqual(response.status_code, 200)
