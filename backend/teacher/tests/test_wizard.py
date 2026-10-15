from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from classrooms.models import Classroom
from questions.models import Question, QuestionType

from core.tests.factories import create_subject
from teacher.wizard import (
    STEP_BASIC,
    STEP_SUBJECT_CLASSROOMS,
    STEP_QUESTIONS,
    STEP_SETTINGS,
    STEP_REVIEW,
)

User = get_user_model()


class TeacherWizardTestCase(TestCase):
    """Test case for the teacher competition wizard."""

    def setUp(self):
        """Create test data."""
        User.objects.filter(username="teacher").delete()
        self.user = User.objects.create_user(
            username="teacher",
            email="teacher@example.com",
            password="testpass123",
            role="teacher",
        )
        self.classroom1, _ = Classroom.objects.get_or_create(
            name="Classroom A",
            defaults={"grade": "Grade 7", "active": True},
        )
        self.classroom1.teachers.add(self.user)
        self.classroom2, _ = Classroom.objects.get_or_create(
            name="Classroom B",
            defaults={"grade": "Grade 8", "active": True},
        )
        self.classroom2.teachers.add(self.user)
        self.subject = create_subject(name="Science", slug="science")
        self.question1, _ = Question.objects.get_or_create(
            text="What is 2+2? Science question",
            defaults={
                "subject": self.subject,
                "question_type": QuestionType.MULTIPLE_CHOICE,
                "duration_seconds": 30,
                "active": True,
            },
        )
        self.question2, _ = Question.objects.get_or_create(
            text="What is the capital of France? Geography question",
            defaults={
                "subject": self.subject,
                "question_type": QuestionType.MULTIPLE_CHOICE,
                "duration_seconds": 45,
                "active": True,
            },
        )
        self.client.login(username="teacher", password="testpass123")

    def test_wizard_get_basic_step(self):
        """Test that the wizard loads the basic information step."""
        url = reverse("teacher:create_competition")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "teacher/wizard.html")
        self.assertContains(response, "Step 1 — Basic Information")
        self.assertEqual(response.context["step"], STEP_BASIC)

    def test_wizard_post_basic_step_valid(self):
        """Test submitting valid basic information advances to classrooms step."""
        url = reverse("teacher:create_competition")
        response = self.client.post(
            url,
            {
                "title": "Test Competition",
                "subject": "Science",
                "grade": "Grade 7",
                "description": "A test competition",
                "action": "next",
            },
        )
        self.assertRedirects(response, url)
        session = self.client.session
        wizard_data = session.get("teacher_competition_wizard", {})
        self.assertEqual(wizard_data.get("step"), STEP_SUBJECT_CLASSROOMS)
        self.assertEqual(
            wizard_data.get("basic", {}).get("title"), "Test Competition"
        )

    def test_wizard_post_basic_step_invalid(self):
        """Test submitting invalid basic information shows errors."""
        url = reverse("teacher:create_competition")
        response = self.client.post(
            url,
            {
                "title": "",
                "action": "next",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A competition name is required.")
        self.assertEqual(response.context["step"], STEP_BASIC)
        self.assertTrue(response.context["errors"])

    def test_wizard_post_subject_and_classrooms_step(self):
        """Test submitting classroom selection advances to settings step."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_SUBJECT_CLASSROOMS,
            "basic": {
                "title": "Test Competition",
                "subject": "Science",
                "grade": "Grade 7",
                "description": "",
            },
            "classroom_ids": [],
        }
        session.save()
        url = reverse("teacher:create_competition")
        response = self.client.post(
            url,
            {
                "subject_id": self.subject.pk,
                "classroom_ids": [self.classroom1.pk, self.classroom2.pk],
                "action": "next",
            },
        )
        self.assertRedirects(response, url)
        session = self.client.session
        wizard_data = session.get("teacher_competition_wizard", {})
        self.assertEqual(wizard_data.get("step"), STEP_QUESTIONS)
        actual_ids = {int(qid) for qid in wizard_data.get("classroom_ids", [])}
        self.assertEqual(actual_ids, {self.classroom1.pk, self.classroom2.pk})

    def test_wizard_post_settings_step(self):
        """Test submitting settings advances to review step."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_SETTINGS,
            "basic": {
                "title": "Test Competition",
                "subject": "Science",
                "grade": "Grade 7",
                "description": "",
            },
            "classroom_ids": [self.classroom1.pk],
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
            },
        }
        session.save()
        url = reverse("teacher:create_competition")
        response = self.client.post(
            url,
            {
                "duration_seconds": 45,
                "scoring_mode": "standard",
                "speed_bonus": 15,
                "action": "next",
            },
        )
        self.assertRedirects(response, url)
        session = self.client.session
        wizard_data = session.get("teacher_competition_wizard", {})
        self.assertEqual(wizard_data.get("step"), STEP_REVIEW)
        self.assertEqual(
            wizard_data.get("settings", {}).get("duration_seconds"), 45
        )
        self.assertEqual(
            wizard_data.get("settings", {}).get("speed_bonus"), 15
        )

    def test_wizard_post_questions_step(self):
        """Test submitting question selection advances to review step."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_QUESTIONS,
            "basic": {
                "title": "Test Competition",
                "subject": "Science",
                "grade": "Grade 7",
                "description": "",
            },
            "classroom_ids": [self.classroom1.pk],
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
            },
            "question_ids": [],
        }
        session.save()
        url = reverse("teacher:create_competition")
        response = self.client.post(
            url,
            {
                "question_ids": [self.question1.pk, self.question2.pk],
                "action": "next",
            },
        )
        self.assertRedirects(response, url)
        session = self.client.session
        wizard_data = session.get("teacher_competition_wizard", {})
        self.assertEqual(wizard_data.get("step"), STEP_SETTINGS)
        actual_ids = {int(qid) for qid in wizard_data.get("question_ids", [])}
        self.assertEqual(actual_ids, {self.question1.pk, self.question2.pk})

    def test_wizard_post_questions_step_invalid(self):
        """Test submitting no questions shows error."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_QUESTIONS,
            "basic": {
                "title": "Test Competition",
                "subject": "Science",
                "grade": "Grade 7",
                "description": "",
            },
            "classroom_ids": [self.classroom1.pk],
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
            },
            "question_ids": [],
        }
        session.save()
        url = reverse("teacher:create_competition")
        response = self.client.post(url, {"action": "next"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select at least one question.")
        self.assertEqual(response.context["step"], STEP_QUESTIONS)
        self.assertTrue(response.context["errors"])

    def test_wizard_review_step(self):
        """Test that the review step displays all entered data."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_REVIEW,
            "basic": {
                "title": "Test Competition",
                "subject": "Science",
                "grade": "Grade 7",
                "description": "A test competition",
            },
            "classroom_ids": [self.classroom1.pk],
            "subject_id": self.subject.pk,
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
            },
            "question_ids": [self.question1.pk],
        }
        session.save()
        url = reverse("teacher:create_competition")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "teacher/wizard.html")
        self.assertContains(response, "Step 5 — Review")
        self.assertEqual(response.context["step"], STEP_REVIEW)
        self.assertEqual(response.context["review_title"], "Test Competition")
        self.assertEqual(response.context["review_subject_name"], "Science")
        self.assertEqual(response.context["review_grade"], "Grade 7")
        self.assertEqual(
            response.context["review_description"], "A test competition"
        )
        self.assertEqual(response.context["review_question_count"], 1)

    def test_wizard_save_draft(self):
        """Test saving the wizard state as a draft competition."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_REVIEW,
            "basic": {
                "title": "Test Competition",
                "subject": "Science",
                "grade": "Grade 7",
                "description": "A test competition",
            },
            "classroom_ids": [self.classroom1.pk],
            "subject_id": self.subject.pk,
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
            },
            "question_ids": [self.question1.pk],
        }
        session.save()
        from competitions.models import Competition, CompetitionState
        initial_count = Competition.objects.count()
        url = reverse("teacher:create_competition")
        response = self.client.post(url, {"action": "save_draft"})
        self.assertEqual(Competition.objects.count(), initial_count + 1)
        competition = Competition.objects.first()
        self.assertRedirects(response, reverse("teacher:competition_detail", args=[competition.pk]))
        competition = Competition.objects.first()
        self.assertEqual(competition.title, "Test Competition")
        self.assertEqual(competition.teacher, self.user)
        self.assertEqual(competition.state, CompetitionState.WAITING)
        session = self.client.session
        wizard_data = session.get("teacher_competition_wizard", {})
        self.assertEqual(wizard_data, {})

    def test_wizard_create_competition(self):
        """Test creating a competition from the wizard."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_REVIEW,
            "basic": {
                "title": "Test Competition",
                "subject": "Science",
                "grade": "Grade 7",
                "description": "A test competition",
            },
            "classroom_ids": [self.classroom1.pk],
            "subject_id": self.subject.pk,
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
            },
            "question_ids": [self.question1.pk],
        }
        session.save()
        from competitions.models import Competition, CompetitionState
        initial_count = Competition.objects.count()
        url = reverse("teacher:create_competition")
        response = self.client.post(url, {"action": "submit"})
        self.assertEqual(Competition.objects.count(), initial_count + 1)
        competition = Competition.objects.first()
        self.assertRedirects(response, reverse("teacher:competition_detail", args=[competition.pk]))
        competition = Competition.objects.first()
        self.assertEqual(competition.title, "Test Competition")
        self.assertEqual(competition.teacher, self.user)
        self.assertEqual(competition.state, CompetitionState.WAITING)
        session = self.client.session
        wizard_data = session.get("teacher_competition_wizard", {})
        self.assertEqual(wizard_data, {})

    def test_anonymous_denied(self):
        """Test that anonymous users cannot access the wizard."""
        self.client.logout()
        url = reverse("teacher:create_competition")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_question_search(self):
        """Test that question search filters correctly."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_QUESTIONS,
            "basic": {"title": "Test", "subject": "", "grade": "", "description": ""},
            "classroom_ids": [],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard", "speed_bonus": 10},
            "question_ids": [],
        }
        session.save()
        url = reverse("teacher:create_competition") + "?q=capital"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["q"], "capital")
        self.assertEqual(len(response.context["questions"]), 1)
        self.assertEqual(
            response.context["questions"][0].text,
            "What is the capital of France? Geography question",
        )

    def test_question_type_filter(self):
        """Test that question type filtering works."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_QUESTIONS,
            "basic": {"title": "Test", "subject": "", "grade": "", "description": ""},
            "classroom_ids": [],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard", "speed_bonus": 10},
            "question_ids": [],
        }
        session.save()
        url = reverse("teacher:create_competition") + "?type=multiple_choice"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["question_type"], "multiple_choice")
        self.assertEqual(len(response.context["questions"]), 2)

    def test_question_search_clear(self):
        """Test that clearing search returns all questions."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_QUESTIONS,
            "basic": {"title": "Test", "subject": "", "grade": "", "description": ""},
            "classroom_ids": [],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard", "speed_bonus": 10},
            "question_ids": [],
        }
        session.save()
        url = reverse("teacher:create_competition") + "?q=&type="
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["q"], "")
        self.assertEqual(len(response.context["questions"]), 2)


class TeacherWizardScoringAndSchedulingTests(TestCase):
    """Scoring, per-question overrides, review totals and the wizard schedule.

    These drive the real POST flow (not just the view's internals) and assert on
    what is persisted: the default points, each question's own points, the stored
    scoring rule, the round total, and that scheduling alone never starts it.
    """

    def setUp(self):
        User.objects.filter(username="scorer").delete()
        self.user = User.objects.create_user(
            username="scorer", password="testpass123", role="teacher"
        )
        self.classroom = Classroom.objects.create(name="Scoring Room", active=True)
        self.classroom.teachers.add(self.user)
        self.subject = create_subject(name="Scoring", slug="scoring")
        self.questions = [
            Question.objects.create(
                text=f"Scoring question {i}", subject=self.subject,
                question_type=QuestionType.MULTIPLE_CHOICE, duration_seconds=30,
                active=True,
            )
            for i in range(1, 6)
        ]
        self.client.login(username="scorer", password="testpass123")

    def _seed(self, session, **overrides):
        data = {
            "step": STEP_REVIEW,
            "basic": {
                "title": "Scoring Round",
                "subject": "",
                "grade": "",
                "description": "",
            },
            "classroom_ids": [self.classroom.pk],
            "subject_id": self.subject.pk,
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
                "points_per_question": 100,
                "planned_for": "",
            },
            "question_ids": [str(q.pk) for q in self.questions],
            "question_points": {},
        }
        data.update(overrides)
        session["teacher_competition_wizard"] = data
        session.save()
        return data

    def test_default_points_are_computed_on_the_server_and_persisted(self):
        """Five questions at two points each is a total of ten, persisted."""
        from competitions.models import Competition, CompetitionQuestion
        from scoring.models import ScoringRule, DEFAULT_CORRECT_POINTS

        session = self.client.session
        self._seed(
            session,
            settings={
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
                "points_per_question": 2,
            },
        )
        response = self.client.post(
            reverse("teacher:create_competition"), {"action": "submit"}
        )
        competition = Competition.objects.get(title="Scoring Round")

        # The server-calculated total, not a browser-sent number.
        self.assertEqual(competition.total_points, 10)

        # Persisted per-question points and the stored scoring rule agree with it.
        rows = list(competition.questions.all())
        self.assertEqual([r.points for r in rows], [2, 2, 2, 2, 2])
        rule = ScoringRule.objects.get(competition=competition)
        self.assertEqual(rule.correct_points, 2)

        # Not started, not live - just created.
        self.assertFalse(competition.is_started)
        self.assertFalse(competition.is_live)
        self.assertIsNone(competition.started_at)

    def test_default_points_fall_back_when_the_step_left_no_value(self):
        """A session with no points_per_question uses the documented default."""
        from competitions.models import Competition
        from scoring.models import ScoringRule, DEFAULT_CORRECT_POINTS

        session = self.client.session
        # Deliberately omit points_per_question from the stored settings.
        data = self._seed(session)
        data["settings"].pop("points_per_question", None)
        session.save()

        Competition.objects.filter(title="Scoring Round").delete()
        self.client.post(reverse("teacher:create_competition"), {"action": "submit"})

        competition = Competition.objects.get(title="Scoring Round")
        self.assertEqual(competition.total_points, 5 * DEFAULT_CORRECT_POINTS)
        self.assertEqual(
            ScoringRule.objects.get(competition=competition).correct_points,
            DEFAULT_CORRECT_POINTS,
        )

    def test_a_per_question_override_changes_only_that_question(self):
        """An override replaces its own question's points and the total."""
        from competitions.models import Competition, CompetitionQuestion

        session = self.client.session
        target = self.questions[2]
        self._seed(
            session,
            settings={
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
                "points_per_question": 2,
            },
            question_points={str(target.pk): 7},
        )
        self.client.post(reverse("teacher:create_competition"), {"action": "submit"})

        competition = Competition.objects.get(title="Scoring Round")
        by_id = {r.question_id: r.points for r in competition.questions.all()}
        self.assertEqual(by_id[target.pk], 7)
        self.assertEqual(by_id[self.questions[0].pk], 2)
        # Four at 2, one at 7 => 8 + 7 = 15, computed from the real questions.
        self.assertEqual(competition.total_points, 15)

    def test_invalid_per_question_points_are_rejected_and_nothing_is_created(self):
        """A non-numeric override blocks the step and never creates a round."""
        from competitions.models import Competition

        Competition.objects.filter(title="Scoring Round").delete()
        session = self.client.session
        self._seed(session, step=STEP_QUESTIONS)
        target = self.questions[0]
        response = self.client.post(
            reverse("teacher:create_competition"),
            {
                "action": "next",
                "question_ids": [q.pk for q in self.questions],
                f"points_{target.pk}": "not-a-number",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["step"], STEP_QUESTIONS)
        self.assertTrue(response.context["errors"])
        self.assertFalse(Competition.objects.filter(title="Scoring Round").exists())

    def test_over_max_per_question_points_are_rejected(self):
        from competitions.models import Competition
        from scoring.models import MAX_CORRECT_POINTS

        Competition.objects.filter(title="Scoring Round").delete()
        session = self.client.session
        self._seed(session, step=STEP_QUESTIONS)
        response = self.client.post(
            reverse("teacher:create_competition"),
            {
                "action": "next",
                "question_ids": [q.pk for q in self.questions],
                f"points_{self.questions[0].pk}": str(MAX_CORRECT_POINTS + 1),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["step"], STEP_QUESTIONS)
        self.assertTrue(response.context["errors"])
        self.assertFalse(Competition.objects.filter(title="Scoring Round").exists())

    def test_the_review_step_renders_the_total_possible_score(self):
        """The review page (a fixed regression: it once raised a NameError) shows
        the server-calculated total, not just a hard-coded number."""
        session = self.client.session
        self._seed(
            session,
            settings={
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
                "points_per_question": 2,
            },
        )
        response = self.client.get(reverse("teacher:create_competition"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Total possible score")
        self.assertContains(response, "Points per question")
        # Five questions at two points each => the total of ten is on the page.

    def test_scheduling_inside_the_wizard_does_not_start_it(self):
        """A planned time saved through the wizard records intent, never a start."""
        from competitions.models import Competition

        Competition.objects.filter(title="Scoring Round").delete()
        # Walk the settings step the way the browser does.
        session = self.client.session
        self._seed(session, step=STEP_SETTINGS)
        response = self.client.post(
            reverse("teacher:create_competition"),
            {
                "action": "next",
                "duration_seconds": "30",
                "scoring_mode": "standard",
                "speed_bonus": "10",
                "points_per_question": "3",
                "planned_for": "2030-01-01 10:30",
            },
        )
        self.assertEqual(response.status_code, 302)  # advanced to review

        response = self.client.post(
            reverse("teacher:create_competition"), {"action": "save_draft"}
        )
        competition = Competition.objects.get(title="Scoring Round")
        competition.refresh_from_db()
        self.assertIsNotNone(competition.planned_for)
        self.assertEqual(competition.lifecycle_label, "planned")
        self.assertFalse(competition.is_started)
        self.assertFalse(competition.is_live)
        self.assertIsNone(competition.started_at)