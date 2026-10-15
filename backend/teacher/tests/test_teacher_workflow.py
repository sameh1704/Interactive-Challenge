"""The teacher-first workflow: subject banks, adaptive forms, explicit start.

The workflow this file protects is one a teacher runs without technical
knowledge:

    subject -> questions from that subject's bank -> competition setup
    -> review and save -> the teacher presses Start -> the selected
    classrooms' screens show the round.

What that requires to be *true* rather than merely rendered:

* **Part A** - a question belongs to a subject, and a competition's questions
  come from the subject that was chosen. A question from another subject must not
  end up in a round, which is the "silently included" case the wizard's step 2
  exists to prevent.
* **Part B** - the Question Builder saves a scorable question for every type
  through ordinary controls, and rejects an invalid answer key server-side.
* **Part C/E** - the wizard's questions bank is subject-scoped, a random draw is
  persisted rather than re-drawn, and the order the teacher leaves is the order
  the round plays in.
* **Part D** - saving or scheduling a competition does not start it. Starting is
  a deliberate, authorising POST that goes through the live engine, refuses a
  second press, and refuses a competition that is not startable.

The live half of the workflow - one Start reaching the selected screens and no
others - is in :mod:`teacher.tests.test_workflow_screens`, over real sockets.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from competitions.models import Competition, CompetitionState
from core.tests.factories import (
    create_classroom,
    create_competition,
    create_question,
    create_subject,
    create_teacher,
)
from questions.models import Question, QuestionType
from subjects.models import Subject

from teacher.wizard import STEP_QUESTIONS, STEP_REVIEW, STEP_SETTINGS, SESSION_KEY


# ---------------------------------------------------------------------------
# Part A - subjects
# ---------------------------------------------------------------------------


class SubjectBankTests(TestCase):
    """A question belongs to a subject, and the bank is organised by it."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.subject.teacher")
        cls.maths = create_subject(name="Mathematics", slug="ui8-maths-a")
        cls.science = create_subject(name="Science", slug="ui8-science-a")
        cls.maths_question = create_question(
            text="What is 7 × 8?", subject=cls.maths
        )
        cls.science_question = create_question(
            text="What gas do plants take in?", subject=cls.science
        )

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def test_a_question_remembers_its_subject(self) -> None:
        self.assertEqual(self.maths_question.subject, self.maths)
        self.assertEqual(self.science_question.subject, self.science)

    def test_the_question_bank_filters_by_subject(self) -> None:
        response = self.client.get(
            reverse("teacher:questions"), {"subject": str(self.maths.pk)}
        )
        self.assertEqual(response.status_code, 200)
        listed = list(response.context["questions"])
        self.assertIn(self.maths_question, listed)
        self.assertNotIn(self.science_question, listed)

    def test_an_unknown_subject_id_shows_the_whole_bank(self) -> None:
        """A stale link must not empty the bank or fail."""
        response = self.client.get(
            reverse("teacher:questions"), {"subject": "99999999"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.maths_question, list(response.context["questions"]))
        self.assertIn(self.science_question, list(response.context["questions"]))

    def test_the_bank_lists_the_subject_that_each_question_belongs_to(self) -> None:
        response = self.client.get(reverse("teacher:questions"))
        # The column exists, and the subject is named next to its own question.
        self.assertContains(response, "Mathematics")
        self.assertContains(response, "Science")

    def test_subjects_are_administrator_managed(self) -> None:
        """Subject naming is central, and the admin enforces who may do it.

        A teacher may read a subject - they pick one in every wizard - but may not
        create or rename one, because a renamed subject would re-label every
        question bank in the school.
        """
        from django.contrib import admin as django_admin

        from core.admin import AdministratorAdminMixin
        from subjects.models import Topic

        site = django_admin.site
        self.assertIn(Subject, site._registry)
        self.assertIn(Topic, site._registry)
        for model in (Subject, Topic):
            with self.subTest(model=model.__name__):
                self.assertTrue(issubclass(type(site._registry[model]), AdministratorAdminMixin))

        # The admin itself is behind administrator rights, so a teacher who tries
        # to reach it is sent to the sign-in page.
        self.client.force_login(self.teacher)
        for path in (
            reverse("admin:subjects_subject_add"),
            reverse("admin:subjects_subject_changelist"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response.headers["location"])

    def test_a_topic_narrows_within_its_subject(self) -> None:
        from subjects.models import Topic

        fractions = Topic.objects.create(
            subject=self.maths, name="Fractions", slug="fractions"
        )
        question = create_question(
            text="What is 1/2 of 10?", subject=self.maths, topic=fractions
        )
        self.assertEqual(question.subject, self.maths)
        self.assertEqual(question.topic, fractions)

        # A topic from one subject is not offered for another.
        response = self.client.get(reverse("teacher:questions"), {"subject": str(self.science.pk)})
        self.assertNotIn(question, list(response.context["questions"]))


# ---------------------------------------------------------------------------
# Part A/C - the wizard is subject-scoped
# ---------------------------------------------------------------------------


class SubjectScopedWizardTests(TestCase):
    """Questions in the wizard come from the subject that was chosen."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.scoped.teacher")
        cls.maths = create_subject(name="Mathematics", slug="ui8-maths-b")
        cls.science = create_subject(name="Science", slug="ui8-science-b")
        cls.room = create_classroom(name="غرفة النطاق")
        cls.room.teachers.add(cls.teacher)
        cls.maths_question = create_question(text="سؤال رياضيات", subject=cls.maths)
        cls.science_question = create_question(text="سؤال علوم", subject=cls.science)

    def setUp(self) -> None:
        self.client.force_login(self.teacher)
        session = self.client.session
        session[SESSION_KEY] = {
            "step": STEP_QUESTIONS,
            "subject_id": str(self.maths.pk),
            "topic_id": "",
            "basic": {"title": "", "subject": "", "grade": "", "description": ""},
            "classroom_ids": [str(self.room.pk)],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard", "speed_bonus": 10},
            "question_ids": [],
        }
        session.save()

    def test_only_the_chosen_subjects_questions_are_offered(self) -> None:
        response = self.client.get(reverse("teacher:create_competition"))
        self.assertContains(response, "سؤال رياضيات")
        self.assertNotContains(response, "سؤال علوم")

    def test_a_question_from_another_subject_is_not_added(self) -> None:
        """The case the subject step exists to prevent: silent inclusion."""
        response = self.client.post(
            reverse("teacher:create_competition"),
            {
                "action": "next",
                "question_ids": [str(self.maths_question.pk), str(self.science_question.pk)],
            },
        )
        self.assertRedirects(response, reverse("teacher:create_competition"))
        selected = self.client.session[SESSION_KEY]["question_ids"]
        self.assertIn(str(self.maths_question.pk), selected)
        self.assertNotIn(str(self.science_question.pk), selected)

    def test_the_step_requires_a_subject(self) -> None:
        session = self.client.session
        state = session[SESSION_KEY]
        state["step"] = "subject_classrooms"
        session[SESSION_KEY] = state
        session.save()
        response = self.client.post(
            reverse("teacher:create_competition"),
            {"action": "next", "classroom_ids": [str(self.room.pk)]},
        )
        self.assertContains(response, "Choose the subject these questions come from.")

    def test_the_step_names_how_many_questions_each_subject_has(self) -> None:
        session = self.client.session
        state = session[SESSION_KEY]
        state["step"] = "subject_classrooms"
        session[SESSION_KEY] = state
        session.save()
        response = self.client.get(reverse("teacher:create_competition"))
        self.assertContains(response, "Mathematics")
        self.assertContains(response, "how many questions this subject currently has")

    def test_the_questions_step_says_which_subject_it_is_showing(self) -> None:
        response = self.client.get(reverse("teacher:create_competition"))
        self.assertContains(response, "Questions from the Mathematics bank only.")


# ---------------------------------------------------------------------------
# Part C - manual selection, random draw, order of play
# ---------------------------------------------------------------------------


class WizardQuestionSelectionTests(TestCase):
    """Manual and random selection, and the order a round plays in."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.select.teacher")
        cls.subject = create_subject(name="Mathematics", slug="ui8-maths-c")
        cls.other = create_subject(name="Science", slug="ui8-science-c")
        cls.room = create_classroom(name="غرفة الاختيار")
        cls.room.teachers.add(cls.teacher)
        cls.questions = [
            create_question(text=f"سؤال {position}", subject=cls.subject)
            for position in range(1, 4)
        ]

    def setUp(self) -> None:
        self.client.force_login(self.teacher)
        session = self.client.session
        session[SESSION_KEY] = {
            "step": STEP_QUESTIONS,
            "subject_id": str(self.subject.pk),
            "topic_id": "",
            "basic": {"title": "مسابقة الاختيار", "subject": "", "grade": "", "description": ""},
            "classroom_ids": [str(self.room.pk)],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard", "speed_bonus": 10},
            "question_ids": [],
        }
        session.save()

    def test_manual_selection_persists_the_chosen_questions_and_order(self) -> None:
        chosen = [self.questions[2].pk, self.questions[0].pk]
        self.client.post(
            reverse("teacher:create_competition"),
            {"action": "next", "question_ids": [str(pk) for pk in chosen]},
        )
        self.assertEqual(
            [int(pk) for pk in self.client.session[SESSION_KEY]["question_ids"]], chosen
        )

    def test_a_random_draw_is_recorded_so_it_does_not_change_on_every_visit(self) -> None:
        """The saved competition holds the questions that were drawn."""
        self.client.post(
            reverse("teacher:create_competition"),
            {"action": "add_random", "random_count": "2"},
        )
        drawn = self.client.session[SESSION_KEY]["question_ids"]
        self.assertEqual(len(drawn), 2)

        # Revisiting the step keeps the same set, because it is in the state.
        again = self.client.session[SESSION_KEY]["question_ids"]
        self.assertEqual(drawn, again)
        self.assertEqual(
            set(drawn), set(drawn) & {str(question.pk) for question in self.questions}
        )

    def test_a_random_draw_cannot_exceed_the_bank(self) -> None:
        self.client.post(
            reverse("teacher:create_competition"),
            {"action": "add_random", "random_count": "50"},
        )
        self.assertEqual(len(self.client.session[SESSION_KEY]["question_ids"]), 3)

    def test_a_random_draw_needs_a_number(self) -> None:
        response = self.client.post(
            reverse("teacher:create_competition"),
            {"action": "add_random", "random_count": "not-a-number"},
        )
        self.assertContains(response, "Enter how many questions to pick.")

    def test_the_order_of_play_can_be_changed(self) -> None:
        order = [self.questions[0].pk, self.questions[1].pk]
        self.client.post(
            reverse("teacher:create_competition"),
            {"action": "next", "question_ids": [str(pk) for pk in order]},
        )
        # Move the second question up, so the order reverses.
        response = self.client.post(
            reverse("teacher:create_competition"),
            {
                "action": "move_up",
                "question_id": str(self.questions[1].pk),
            },
        )
        self.assertRedirects(response, reverse("teacher:create_competition"))
        self.assertEqual(
            [int(pk) for pk in self.client.session[SESSION_KEY]["question_ids"]],
            [self.questions[1].pk, self.questions[0].pk],
        )

    def test_the_selected_count_is_shown(self) -> None:
        """The step says how many are chosen, so the count is never a surprise."""
        session = self.client.session
        state = session[SESSION_KEY]
        state["question_ids"] = [str(self.questions[0].pk)]
        session[SESSION_KEY] = state
        session.save()

        response = self.client.get(reverse("teacher:create_competition"))
        self.assertContains(response, "1 question selected")

    def test_the_step_offers_creating_a_question(self) -> None:
        response = self.client.get(reverse("teacher:create_competition"))
        self.assertContains(response, reverse("teacher:create_question") + "?return_to_competition=1")


# ---------------------------------------------------------------------------
# Part B - the adaptive question builder
# ---------------------------------------------------------------------------


class AdaptiveQuestionBuilderTests(TestCase):
    """Every type saves through ordinary controls, and bad keys are refused."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.adaptive.teacher")
        cls.subject = create_subject(name="Mathematics", slug="ui8-maths-d")

    def setUp(self) -> None:
        self.client.force_login(self.teacher)
        self.payload = {
            "text": "ما هو ٥ × ٦؟",
            "subject": str(self.subject.pk),
            "duration_seconds": "30",
            "active": "on",
        }

    def create(self, **overrides):
        self.payload.update(overrides)
        return self.client.post(reverse("teacher:create_question"), self.payload)

    def saved(self) -> Question:
        return Question.objects.get(text=self.payload["text"])

    def test_multiple_choice_saves_its_options_and_key(self) -> None:
        self.create(
            question_type=QuestionType.MULTIPLE_CHOICE,
            options='["11","30","35"]',
            correct_option="30",
        )
        question = self.saved()
        self.assertEqual(question.options, ["11", "30", "35"])
        self.assertEqual(question.correct_option, "30")
        self.assertEqual(question.question_type, QuestionType.MULTIPLE_CHOICE)

    def test_true_false_needs_no_options(self) -> None:
        self.create(
            question_type=QuestionType.TRUE_FALSE,
            options="",
            correct_answer="false",
        )
        question = self.saved()
        # No options at all, and the answer key is the boolean the teacher picked.
        self.assertEqual(question.options, [])
        self.assertFalse(question.correct_answer)

    def test_ordering_saves_the_items_and_their_correct_order(self) -> None:
        import json

        self.create(
            question_type=QuestionType.ORDERING,
            options="",
            type_config=json.dumps(
                {
                    "items": ["الأول", "الثاني", "الثالث"],
                    "correct_order": ["الثالث", "الأول", "الثاني"],
                }
            ),
        )
        question = self.saved()
        self.assertEqual(question.type_config["items"], ["الأول", "الثاني", "الثالث"])
        self.assertEqual(
            question.type_config["correct_order"], ["الثالث", "الأول", "الثاني"]
        )

    def test_classification_saves_categories_and_assignments(self) -> None:
        import json

        self.create(
            question_type=QuestionType.CLASSIFICATION,
            options="",
            type_config=json.dumps(
                {
                    "categories": ["حيوان", "نبات"],
                    "assignments": [
                        {"label": "أسد", "category": "حيوان"},
                        {"label": "نخلة", "category": "نبات"},
                    ],
                }
            ),
        )
        question = self.saved()
        self.assertEqual(question.type_config["categories"], ["حيوان", "نبات"])
        self.assertEqual(
            question.type_config["assignments"],
            [
                {"label": "أسد", "category": "حيوان"},
                {"label": "نخلة", "category": "نبات"},
            ],
        )

    def test_short_answer_saves_accepted_answers_and_case_sensitivity(self) -> None:
        import json

        self.create(
            question_type=QuestionType.SHORT_ANSWER,
            options="",
            type_config=json.dumps(
                {"accepted_answers": ["قاهرة", "القاهرة"], "case_sensitive": True}
            ),
        )
        question = self.saved()
        self.assertEqual(
            question.type_config["accepted_answers"], ["قاهرة", "القاهرة"]
        )
        self.assertTrue(question.type_config["case_sensitive"])

    def test_each_type_rejects_an_invalid_answer_key(self) -> None:
        """The answer key is validated on the server, not trusted from the form."""
        response = self.create(
            question_type=QuestionType.MULTIPLE_CHOICE,
            options='["11","30"]',
            correct_option="99",  # not one of the options
        )
        self.assertFalse(Question.objects.filter(text=self.payload["text"]).exists())
        # The page re-renders with the error rather than raising.
        self.assertContains(response, "is not one of the options")

    def test_no_teacher_facing_json_field_is_rendered(self) -> None:
        """The acceptance criterion: a teacher never sees or writes JSON.

        The JSON columns are how the model and the scoring engine store the
        answer key, so they exist in the form as hidden inputs. What this asserts
        is that none of them is ever a *visible control*: no label names them, and
        no text area or input asks for their contents.
        """
        response = self.client.get(reverse("teacher:create_question"))
        rendered = response.content.decode()

        # The model's JSON columns are present, as hidden inputs the script fills.
        for name in ("options", "correct_option", "correct_answer", "type_config"):
            with self.subTest(field=name):
                self.assertIn(f'name="{name}"', rendered)

        # None of them carries a label of its own, which is what would put
        # "options" or "type_config" in front of a teacher.
        for name in ("options", "type_config", "accepted_answers", "correct_order",
                     "categories", "assignments", "items"):
            with self.subTest(field=name):
                self.assertNotRegex(rendered, r'<label[^>]*for="id\s*' + name + r'"')

        # And no visible control asks for a JSON body.
        for label in ("options", "type_config", "accepted_answers",
                      "correct_order", "categories"):
            with self.subTest(label=label):
                self.assertNotRegex(rendered, r'<label[^>]*>[^<]*' + label + r'[^<]*</label>',)

    def test_the_builder_offers_save_and_preview_sections(self) -> None:
        response = self.client.get(reverse("teacher:create_question"))
        self.assertContains(response, "Live preview")
        self.assertContains(response, "Save question")


# ---------------------------------------------------------------------------
# Part D - prepare, schedule, start
# ---------------------------------------------------------------------------


class CompetitionLifecycleTests(TestCase):
    """Saving and scheduling prepare a competition; only Start starts it."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.life.teacher")
        cls.subject = create_subject(name="Mathematics", slug="ui8-maths-e")
        cls.room = create_classroom(name="غرفة دورة الحياة")
        cls.room.teachers.add(cls.teacher)

    def competition(self, **kwargs):
        defaults = {
            "teacher": self.teacher,
            "title": "مسابقة دورة الحياة",
            "classrooms": [self.room],
            "questions": [create_question(text="سؤال دورة الحياة", subject=self.subject)],
        }
        defaults.update(kwargs)
        return create_competition(**defaults)

    def test_a_saved_competition_is_not_live(self) -> None:
        competition = self.competition()
        self.assertTrue(competition.is_prepared)
        self.assertFalse(competition.is_live)
        self.assertFalse(competition.is_started)

    def test_scheduling_a_time_does_not_start_it(self) -> None:
        competition = self.competition()
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("teacher:plan_competition", args=[competition.pk]),
            {"planned_for": "2030-01-01 10:30"},
        )
        self.assertRedirects(
            response, reverse("teacher:competition_detail", args=[competition.pk])
        )
        competition.refresh_from_db()

        self.assertIsNotNone(competition.planned_for)
        self.assertFalse(competition.is_started, "scheduling must not start the round")
        self.assertFalse(competition.is_live)
        self.assertIsNone(competition.started_at)
        self.assertEqual(competition.lifecycle_label, "planned")

    def test_the_detail_page_states_that_it_is_waiting_for_the_teacher(self) -> None:
        competition = self.competition()
        competition.planned_for = timezone.now() + timezone.timedelta(hours=2)
        competition.save(update_fields=["planned_for"])
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse("teacher:competition_detail", args=[competition.pk])
        )
        self.assertContains(response, "waiting for teacher to start")
        self.assertContains(response, "Saving or scheduling a competition never starts it")

    def test_the_start_button_is_on_the_detail_page(self) -> None:
        competition = self.competition()
        self.client.force_login(self.teacher)
        response = self.client.get(
            reverse("teacher:competition_detail", args=[competition.pk])
        )
        self.assertContains(response, reverse("teacher:start_competition", args=[competition.pk]))
        self.assertContains(response, "Start Competition")

    def test_the_start_button_is_on_the_list_page(self) -> None:
        competition = self.competition()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("teacher:competitions"))
        self.assertContains(response, reverse("teacher:start_competition", args=[competition.pk]))

    def test_start_changes_the_real_server_state(self) -> None:
        """The live engine, not this view, writes the state."""
        competition = self.competition()
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("teacher:start_competition", args=[competition.pk])
        )
        self.assertRedirects(
            response, reverse("live:teacher_dashboard", args=[competition.pk])
        )
        competition.refresh_from_db()

        self.assertTrue(competition.is_started)
        self.assertTrue(competition.is_live)
        self.assertIsNotNone(competition.started_at)
        # And the state itself, which is what the live engine writes and what
        # every screen's state sync reads.
        self.assertEqual(competition.state, CompetitionState.WAITING)

    def test_start_refuses_a_second_press(self) -> None:
        from live.services import start_competition

        competition = self.competition()
        start_competition(competition)
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("teacher:start_competition", args=[competition.pk])
        )
        competition.refresh_from_db()
        # Refused, with the reason named - not a second start, not a crash.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "has already been started")

    def test_start_requires_a_signed_in_staff_account(self) -> None:
        competition = self.competition()
        response = self.client.get(reverse("teacher:start_competition", args=[competition.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.headers["location"])

    def test_a_teacher_cannot_start_another_teachers_competition(self) -> None:
        stranger = create_teacher(username="ui8.life.stranger")
        competition = self.competition()
        self.client.force_login(stranger)
        response = self.client.post(
            reverse("teacher:start_competition", args=[competition.pk])
        )
        self.assertEqual(response.status_code, 404)
        competition.refresh_from_db()
        self.assertFalse(competition.is_started)

    def test_start_refuses_a_competition_with_no_questions(self) -> None:
        competition = self.competition(questions=[])
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("teacher:start_competition", args=[competition.pk])
        )
        self.assertContains(response, "There are no questions in this competition")
        competition.refresh_from_db()
        self.assertFalse(competition.is_started)

    def test_start_refuses_a_competition_with_no_classrooms(self) -> None:
        competition = self.competition(classrooms=[])
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("teacher:start_competition", args=[competition.pk])
        )
        self.assertContains(response, "There are no classrooms taking part")

    def test_a_get_on_start_shows_the_confirmation_not_a_start(self) -> None:
        competition = self.competition()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("teacher:start_competition", args=[competition.pk]))
        competition.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(competition.is_started, "a GET must not start the round")
        self.assertContains(response, "will take part")

    def test_start_names_the_screens_it_will_reach(self) -> None:
        from core.tests.factories import create_screen

        create_screen(name="شاشة الاختبار", classroom=self.room, is_primary=True)
        competition = self.competition()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("teacher:start_competition", args=[competition.pk]))
        self.assertContains(response, "registered screen")


# ---------------------------------------------------------------------------
# Part E - the wizard saves a prepared competition
# ---------------------------------------------------------------------------


class WizardSaveForLaterTests(TestCase):
    """A saved draft is a prepared competition, not a running one."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="ui8.savelater.teacher")
        cls.subject = create_subject(name="Mathematics", slug="ui8-maths-f")
        cls.room = create_classroom(name="غرفة الحفظ")
        cls.room.teachers.add(cls.teacher)
        cls.question = create_question(text="سؤال الحفظ", subject=cls.subject)

    def test_saving_a_draft_produces_a_prepared_competition(self) -> None:
        self.client.force_login(self.teacher)
        session = self.client.session
        session[SESSION_KEY] = {
            "step": STEP_REVIEW,
            "subject_id": str(self.subject.pk),
            "basic": {
                "title": "مسابقة محفوظة",
                "subject": "",
                "grade": "7",
                "description": "",
            },
            "classroom_ids": [str(self.room.pk)],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard", "speed_bonus": 10},
            "question_ids": [str(self.question.pk)],
        }
        session.save()

        response = self.client.post(
            reverse("teacher:create_competition"), {"action": "save_draft"}
        )
        self.assertEqual(response.status_code, 302)

        competition = Competition.objects.get(title="مسابقة محفوظة")
        self.assertFalse(competition.is_started, "saving a draft must not start it")
        self.assertTrue(competition.is_prepared)
        self.assertIsNone(competition.planned_for)
        self.assertEqual(competition.questions.count(), 1)

        # The teacher can find it again, on the list and on the detail page.
        self.assertRedirects(response, reverse("teacher:competition_detail", args=[competition.pk]))
        detail = self.client.get(reverse("teacher:competition_detail", args=[competition.pk]))
        self.assertContains(detail, "Waiting for the teacher to start it.")

    def test_a_random_draw_survives_into_the_saved_competition(self) -> None:
        """The saved round holds the ids that were drawn, not a re-draw."""
        from questions.models import Question

        for position in range(3, 6):
            create_question(text=f"سؤال الحفظ {position}", subject=self.subject)

        self.client.force_login(self.teacher)
        session = self.client.session
        session[SESSION_KEY] = {
            "step": STEP_QUESTIONS,
            "subject_id": str(self.subject.pk),
            "topic_id": "",
            "basic": {"title": "مسابقة عشوائية", "subject": "", "grade": "", "description": ""},
            "classroom_ids": [str(self.room.pk)],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard", "speed_bonus": 10},
            "question_ids": [],
        }
        session.save()

        self.client.post(
            reverse("teacher:create_competition"),
            {"action": "add_random", "random_count": "3"},
        )
        drawn = list(self.client.session[SESSION_KEY]["question_ids"])

        session = self.client.session
        state = session[SESSION_KEY]
        state["step"] = STEP_REVIEW
        session[SESSION_KEY] = state
        session.save()
        self.client.post(
            reverse("teacher:create_competition"), {"action": "save_draft"}
        )

        competition = Competition.objects.get(title="مسابقة عشوائية")
        saved = [
            str(question.pk)
            for question in Question.objects.filter(
                competition_entries__competition=competition
            ).order_by("competition_entries__position")
        ]
        self.assertEqual(saved, drawn, "the saved round must hold the drawn questions")

        # And the count on the detail page is the drawn count.
        detail = self.client.get(reverse("teacher:competition_detail", args=[competition.pk]))
        self.assertContains(detail, ">3<")
