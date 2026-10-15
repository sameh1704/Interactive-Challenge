"""The teacher question bank: a real, usable, persistent feature.

Questions are a **shared bank** organised by subject, not a per-teacher
collection and not something attached to a competition. This module tests the
bank as a teacher meets it:

* it is reachable through the teacher's own navigation
* it searches, filters by subject/topic/type/status, and pages
* a question can be created, viewed, edited, retired and restored
* invalid input is refused server-side, with the entered values preserved
* nobody outside staff reaches it, and no teacher gains administrator or screen
  powers along the way
* the six existing production questions and their relationships are untouched

The type-specific forms are covered in `test_question_builder.py`, which owns the
Question Builder itself; this file owns the bank around it.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from competitions.models import CompetitionQuestion
from core.tests.factories import (
    TEST_PASSWORD,
    create_administrator,
    create_question,
    create_subject,
    create_teacher,
)
from questions.models import Question, QuestionType
from subjects.models import Subject, Topic

BANK_URL = reverse("teacher:questions")


class QuestionBankAccessTests(TestCase):
    """Who may reach the bank, and how."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui7.bank.teacher")
        cls.administrator = create_administrator(username="ui7.bank.admin")
        cls.question = create_question(text="What is 2 + 2?", subject=create_subject())

    def test_a_teacher_may_reach_the_bank(self) -> None:
        self.client.force_login(self.teacher)
        response = self.client.get(BANK_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "teacher/questions.html")
        self.assertContains(response, "Question Bank")

    def test_an_administrator_may_reach_the_bank(self) -> None:
        self.client.force_login(self.administrator)
        self.assertEqual(self.client.get(BANK_URL).status_code, 200)

    def test_an_anonymous_visitor_is_sent_to_sign_in(self) -> None:
        response = self.client.get(BANK_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.headers["location"])

    def test_the_database_refuses_an_account_with_an_unknown_role(self) -> None:
        """No row outside the two roles can exist in the first place.

        This is the guard that makes the role check a second line of defence
        rather than the first: the check constraint on ``accounts_user`` rejects
        a third role at write time, so ``is_staff_member`` never has to meet one
        that it would have to reason about.
        """
        from django.contrib.auth import get_user_model
        from django.db import IntegrityError, transaction

        User = get_user_model()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User(
                    username="ui7.bank.stranger",
                    role="warehouse",
                ).save()

    def test_the_role_helper_refuses_anything_outside_the_two_roles(self) -> None:
        """``is_staff_member`` itself is the second line of defence."""
        from accounts.permissions import is_staff_member

        for role in ("", "warehouse", "admin", "Administrator"):
            with self.subTest(role=role):
                user = create_teacher(username=f"ui7.role.{role or 'blank'}")
                user.role = role
                self.assertFalse(is_staff_member(user))

    def test_the_bank_is_reachable_from_the_teacher_navigation(self) -> None:
        self.client.force_login(self.teacher)
        content = self.client.get(reverse("teacher:dashboard")).content.decode()
        self.assertIn(reverse("teacher:questions"), content)
        self.assertIn("Question Bank", content)

    def test_the_add_question_action_is_on_the_bank_page(self) -> None:
        self.client.force_login(self.teacher)
        content = self.client.get(BANK_URL).content.decode()
        self.assertIn(reverse("teacher:create_question"), content)
        self.assertIn("Add Question", content)


class QuestionBankFilterTests(TestCase):
    """Every filter the bank offers, and what each one means."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui7.filter.teacher")
        maths = create_subject(name="Mathematics", slug="ui7-maths")
        science = create_subject(name="Science", slug="ui7-science")
        cls.maths = maths
        cls.maths_question = create_question(text="What is 3 x 4?", subject=maths)
        cls.maths_typed_question = create_question(
            text="Water boils at what temperature?",
            subject=maths,
            question_type=QuestionType.SHORT_ANSWER,
        )
        cls.real_question = create_question(
            text="Arrange the planets from the sun", subject=maths
        )
        cls.answered = create_question(text="What is the capital of Egypt?", subject=maths)
        cls.science_question = create_question(text="What do plants make?", subject=science)
        cls.inactive_question = create_question(
            text="Retire me", subject=maths, active=False
        )

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def bank(self, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return self.client.get(f"{BANK_URL}?{query}" if query else BANK_URL)

    def ids(self, response) -> set[int]:
        return {question.pk for question in response.context["questions"]}

    def test_search_matches_question_text(self):
        response = self.bank(q="capital")
        self.assertEqual(self.ids(response), {self.answered.pk})

    def test_search_is_case_insensitive(self):
        response = self.bank(q="WHAT IS 3 X 4")
        self.assertEqual(self.ids(response), {self.maths_question.pk})

    def test_subject_filter_narrows_the_bank(self):
        response = self.bank(subject=self.maths.pk)
        self.assertIn(self.maths_question.pk, self.ids(response))
        self.assertNotIn(self.science_question.pk, self.ids(response))

    def test_topic_filter_narrows_within_a_subject(self):
        topic = Topic.objects.create(
            subject=self.maths, name="Fractions", slug="fractions"
        )
        topiced = create_question(
            text="What is half of 10?", subject=self.maths, topic=topic
        )
        response = self.bank(topic=topic.pk)
        self.assertEqual(self.ids(response), {topiced.pk})

    def test_type_filter_narrows_by_question_type(self):
        response = self.bank(type=QuestionType.SHORT_ANSWER)
        self.assertEqual(self.ids(response), {self.maths_typed_question.pk})

    def test_the_active_filter_is_the_default(self):
        response = self.bank()
        self.assertNotIn(self.inactive_question.pk, self.ids(response))

    def test_the_inactive_filter_shows_retired_questions(self):
        response = self.bank(status="inactive")
        self.assertEqual(self.ids(response), {self.inactive_question.pk})

    def test_all_shows_every_question(self):
        response = self.bank(status="all")
        self.assertIn(self.inactive_question.pk, self.ids(response))

    def test_filters_combine(self):
        response = self.bank(subject=self.maths.pk, type=QuestionType.SHORT_ANSWER, q="temperature")
        self.assertEqual(self.ids(response), {self.maths_typed_question.pk})

    def test_an_unknown_subject_is_ignored_rather_than_erroring(self):
        response = self.bank(subject="not-a-number")
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.context["questions"]), 0)

    def test_an_unknown_topic_is_ignored_rather_than_erroring(self):
        response = self.bank(topic="99999999")
        self.assertEqual(response.status_code, 200)

    def test_an_empty_bank_shows_a_helpful_empty_state(self):
        Question.objects.all().delete()
        response = self.bank()
        self.assertContains(response, "Your question bank is empty")
        self.assertContains(response, reverse("teacher:create_question"))

    def test_an_empty_filter_result_points_at_the_filter_not_at_a_blank_bank(self):
        """The bank has questions; these are not them.

        Saying "Your question bank is empty" here would send a teacher off to
        create a duplicate of something they already have, so the page must name
        the filter in both the title and the body.
        """
        response = self.bank(q="zzzzznothingmatches")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Your question bank is empty")
        self.assertContains(response, "No questions match this filter")
        self.assertContains(response, "clear the filters")
        # The clear control is on the page, so the fix is one click away.
        self.assertContains(response, "Clear")


class QuestionBankPaginationTests(TestCase):
    """A bank longer than one page stays usable."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui7.page.teacher")
        subject = create_subject(slug="ui7-page")
        for index in range(25):
            create_question(text=f"Page question {index:02d}", subject=subject)

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def test_the_bank_pages(self):
        response = self.client.get(BANK_URL)
        self.assertTrue(response.context["is_paginated"])
        self.assertEqual(response.context["paginator"].num_pages, 2)
        self.assertEqual(len(response.context["questions"]), 20)

    def test_the_second_page_is_reachable(self):
        response = self.client.get(BANK_URL, {"page": "2"})
        self.assertEqual(len(response.context["questions"]), 5)

    def test_an_out_of_range_page_shows_the_last_page(self):
        response = self.client.get(BANK_URL, {"page": "99"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["questions"]), 5)

    def test_a_non_numeric_page_shows_the_first_page(self):
        response = self.client.get(BANK_URL, {"page": "second"})
        self.assertEqual(response.context["page_obj"].number, 1)

    def test_paging_carries_the_filters(self):
        """Clicking "next" must not silently drop the search the teacher made."""
        response = self.client.get(BANK_URL, {"q": "Page question"})
        self.assertEqual(response.context["paginator"].num_pages, 2)
        html = response.content.decode()
        self.assertIn("page=2", html)
        # The search box keeps what was typed, too.
        self.assertIn('value="Page question"', html)


class QuestionManagementTests(TestCase):
    """Creating, viewing, retiring and restoring a question from the bank."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui7.manage.teacher")
        cls.subject = create_subject(name="Mathematics", slug="ui7-manage")

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def test_a_question_can_be_created_and_listed(self):
        response = self.client.post(
            reverse("teacher:create_question"),
            {
                "question_type": "multiple_choice",
                "text": "What is 5 x 6?",
                "subject": self.subject.pk,
                "options": '["11", "30", "35", "56"]',
                "correct_option": "30",
                "duration_seconds": "30",
                "active": "on",
                "save_option": "save",
            },
        )
        self.assertEqual(response.status_code, 302)
        question = Question.objects.get(text="What is 5 x 6?")
        # A question belongs to a subject and to no competition.
        self.assertEqual(question.subject, self.subject)
        self.assertFalse(
            CompetitionQuestion.objects.filter(question=question).exists(),
            "creating a question must not attach it to a competition",
        )
        bank = self.client.get(BANK_URL)
        self.assertIn(question.pk, {q.pk for q in bank.context["questions"]})

    def test_a_question_created_while_a_competition_was_in_progress_is_not_attached(self):
        """A question is standalone even when created from inside a wizard."""
        response = self.client.post(
            reverse("teacher:create_question"),
            {
                "question_type": "true_false",
                "text": "The Nile flows north.",
                "subject": self.subject.pk,
                "options": "",
                "correct_answer": "true",
                "duration_seconds": "30",
                "active": "on",
                "save_option": "save_to_competition",
            },
        )
        question = Question.objects.get(text="The Nile flows north.")
        self.assertFalse(
            CompetitionQuestion.objects.filter(question=question).exists(),
            "the save-to-competition button must only return to the wizard, "
            "never silently bind the question to a round",
        )
        # It lands in the bank, reusable by any competition from now on.
        self.assertIn(
            question.pk,
            {q.pk for q in self.client.get(BANK_URL).context["questions"]},
        )

    def test_a_question_can_be_viewed(self):
        question = create_question(text="View me", subject=self.subject)
        response = self.client.get(
            reverse("teacher:question_detail", kwargs={"pk": question.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "teacher/question_detail.html")
        # The answer key is shown here, because the preview deliberately hides it.
        self.assertContains(response, "Stored answer key")
        self.assertContains(response, question.correct_option)

    def test_a_question_can_be_edited_and_the_change_persists(self):
        question = create_question(
            text="Old text", subject=self.subject, correct_option="b"
        )
        response = self.client.post(
            reverse("teacher:edit_question", kwargs={"pk": question.pk}),
            {
                "question_type": "multiple_choice",
                "text": "New text",
                "subject": self.subject.pk,
                "options": '["a", "b", "c"]',
                "correct_option": "c",
                "duration_seconds": "45",
                "active": "on",
                "save_option": "save",
            },
        )
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.text, "New text")
        self.assertEqual(question.correct_option, "c")
        self.assertEqual(question.duration_seconds, 45)

    def test_editing_preserves_the_question_identity(self):
        """Editing must not create a second row for the same question."""
        question = create_question(text="One row", subject=self.subject)
        self.client.post(
            reverse("teacher:edit_question", kwargs={"pk": question.pk}),
            {
                "question_type": "multiple_choice",
                "text": "Still one row",
                "subject": self.subject.pk,
                "options": '["a", "b", "c"]',
                "correct_option": "a",
                "duration_seconds": "30",
                "active": "on",
                "save_option": "save",
            },
        )
        self.assertEqual(Question.objects.filter(pk=question.pk).count(), 1)
        self.assertFalse(Question.objects.filter(text="One row").exists())

    def test_retiring_keeps_the_question_and_hides_it_from_the_default_view(self):
        question = create_question(text="Retire me", subject=self.subject)
        self.client.post(BANK_URL, {"question_id": str(question.pk)})
        question.refresh_from_db()

        self.assertFalse(question.active)
        self.assertTrue(
            Question.objects.filter(pk=question.pk).exists(),
            "retiring must not delete the question: every competition that used "
            "it still has to be able to read it back",
        )
        # It leaves the default view and can be found under the inactive filter.
        default = self.client.get(BANK_URL)
        self.assertNotIn(question.pk, {q.pk for q in default.context["questions"]})
        inactive = self.client.get(BANK_URL, {"status": "inactive"})
        self.assertIn(question.pk, {q.pk for q in inactive.context["questions"]})

    def test_restoring_returns_a_retired_question_to_the_bank(self):
        question = create_question(text="Bring me back", subject=self.subject, active=False)
        self.client.post(BANK_URL, {"question_id": str(question.pk)})
        question.refresh_from_db()

        self.assertTrue(question.active)
        default = self.client.get(BANK_URL)
        self.assertIn(question.pk, {q.pk for q in default.context["questions"]})

    def test_retiring_is_a_post_not_a_link(self):
        """A URL that changes data is one a crawler could follow."""
        question = create_question(text="Do not retire by GET", subject=self.subject)
        self.client.get(reverse("teacher:edit_question", kwargs={"pk": question.pk}))
        response = self.client.get(
            f"{BANK_URL}?question_id={question.pk}"
        )
        question.refresh_from_db()
        self.assertTrue(question.active, "a GET must never change data")
        self.assertEqual(response.status_code, 200)


class QuestionValidationTests(TestCase):
    """The server refuses what it must, and keeps what the teacher typed."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui7.validate.teacher")
        cls.subject = create_subject(slug="ui7-validate")

    def setUp(self) -> None:
        self.client.force_login(self.teacher)
        self.base = {
            "question_type": "multiple_choice",
            "text": "Valid enough",
            "subject": self.subject.pk,
            "options": '["a", "b", "c"]',
            "correct_option": "b",
            "duration_seconds": "30",
            "active": "on",
            "save_option": "save",
        }

    def post(self, **overrides):
        payload = {**self.base, **overrides}
        return self.client.post(reverse("teacher:create_question"), payload)

    def test_a_missing_subject_is_refused(self):
        response = self.post(subject="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Question.objects.filter(text="Valid enough").exists())

    def test_an_unknown_subject_is_refused(self):
        response = self.post(subject="99999999")
        self.assertFalse(Question.objects.filter(text="Valid enough").exists())

    def test_a_question_with_no_text_is_refused(self):
        self.post(text="   ")
        self.assertFalse(Question.objects.filter(text="Valid enough").exists())

    def test_too_few_options_are_refused(self):
        self.post(options='["only one"]', correct_option="only one")
        self.assertFalse(Question.objects.filter(text="Valid enough").exists())

    def test_a_correct_answer_that_is_not_an_option_is_refused(self):
        self.post(options='["a", "b", "c"]', correct_option="zzz")
        self.assertFalse(Question.objects.filter(text="Valid enough").exists())

    def test_invalid_input_keeps_the_entered_values(self):
        response = self.post(correct_option="zzz")
        self.assertEqual(response.status_code, 200)
        # Nothing is lost, so the teacher corrects one field rather than all of
        # them; and the reason is on the page, not only in the console.
        self.assertContains(response, "Valid enough")
        self.assertContains(response, "is not one of the options")

    def test_a_topic_from_another_subject_is_refused(self):
        """A topic narrows one subject; it must not cross into another."""
        other = create_subject(name="Science", slug="ui7-other")
        foreign = Topic.objects.create(
            subject=other, name="Geology", slug="ui7-geology"
        )
        response = self.post(topic=foreign.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Question.objects.filter(text="Valid enough").exists())


class ExistingQuestionIntegrityTests(TestCase):
    """The bank must not damage what is already in it."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui7.integrity.teacher")
        cls.subject = create_subject(name="Mathematics", slug="ui7-integrity")

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def test_existing_questions_survive_creating_and_editing_others(self):
        """The six production questions must be untouched by the new workflow."""
        existing = [
            create_question(text=f"Existing question {index}", subject=self.subject)
            for index in range(6)
        ]
        before = {
            question.pk: (question.text, question.options, question.correct_option)
            for question in existing
        }

        self.client.force_login(self.teacher)
        # A new question alongside them.
        self.client.post(
            reverse("teacher:create_question"),
            {
                "question_type": "true_false",
                "text": "A new one",
                "subject": self.subject.pk,
                "options": "",
                "correct_answer": "true",
                "duration_seconds": "30",
                "active": "on",
                "save_option": "save",
            },
        )
        # And one of theirs edited.
        self.client.post(
            reverse("teacher:edit_question", kwargs={"pk": existing[0].pk}),
            {
                "question_type": "multiple_choice",
                "text": "Edited only this one",
                "subject": self.subject.pk,
                "options": '["x", "y", "z"]',
                "correct_option": "x",
                "duration_seconds": "30",
                "active": "on",
                "save_option": "save",
            },
        )

        for question in existing[1:]:
            question.refresh_from_db()
            self.assertEqual(
                (question.text, question.options, question.correct_option),
                before[question.pk],
                f"question {question.pk} was changed by an unrelated operation",
            )
        # Six untouched plus the one new question, and nothing deleted on the way.
        self.assertEqual(Question.objects.count(), 7)

    def test_a_question_keeps_working_for_the_competition_that_uses_it(self):
        """A shared question is not owned by the round that first used it."""
        from competitions.models import Competition, CompetitionQuestion

        question = create_question(text="Shared", subject=self.subject)
        competition = Competition.objects.create(
            title="A round that used it", teacher=self.teacher
        )
        CompetitionQuestion.objects.create(
            competition=competition, question=question, position=1
        )

        response = self.client.get(
            reverse("teacher:question_detail", kwargs={"pk": question.pk})
        )
        # The detail page shows the question, not the round: a question's own
        # page must not become a window onto every competition it appears in.
        self.assertContains(response, "Shared")
        self.assertNotContains(response, "A round that used it")


class TeacherCannotReachScreenAdminTests(TestCase):
    """Question powers must not arrive with screen powers."""

    def test_a_teacher_cannot_reach_the_screen_admin(self):
        from django.contrib import admin as django_admin

        from screens.models import InteractiveScreen

        from django.test import RequestFactory

        teacher = create_teacher(username="ui7.noscreen.teacher")
        self.client.force_login(teacher)

        # Django's admin is gated on is_staff, which on this model is a property
        # derived from the role: a teacher is never staff, so the admin refuses
        # them without any list of allowed roles to keep in step.
        self.assertFalse(teacher.is_staff)
        request = RequestFactory().get("/admin/")
        request.user = teacher
        self.assertFalse(django_admin.site.has_permission(request))

        # And no route in the teacher workspace names a screen at all.
        self.assertFalse(hasattr(InteractiveScreen, "teacher_can_create"))

    def test_the_teacher_workspace_has_no_screen_route(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("teacher:manage_screens")
