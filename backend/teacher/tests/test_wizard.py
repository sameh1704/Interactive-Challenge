from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from classrooms.models import Classroom
from questions.models import Question, QuestionType

from teacher.wizard import (
    STEP_BASIC,
    STEP_CLASSROOMS,
    STEP_SETTINGS,
    STEP_QUESTIONS,
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
        self.question1, _ = Question.objects.get_or_create(
            text="What is 2+2? Science question",
            defaults={
                "question_type": QuestionType.MULTIPLE_CHOICE,
                "duration_seconds": 30,
                "active": True,
            },
        )
        self.question2, _ = Question.objects.get_or_create(
            text="What is the capital of France? Geography question",
            defaults={
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
        self.assertEqual(wizard_data.get("step"), STEP_CLASSROOMS)
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

    def test_wizard_post_classrooms_step(self):
        """Test submitting classroom selection advances to settings step."""
        session = self.client.session
        session["teacher_competition_wizard"] = {
            "step": STEP_CLASSROOMS,
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
                "classroom_ids": [self.classroom1.pk, self.classroom2.pk],
                "action": "next",
            },
        )
        self.assertRedirects(response, url)
        session = self.client.session
        wizard_data = session.get("teacher_competition_wizard", {})
        self.assertEqual(wizard_data.get("step"), STEP_SETTINGS)
        actual_ids = {int(qid) for qid in wizard_data.get("classroom_ids", [])}
        self.assertEqual(actual_ids, {self.classroom1.pk, self.classroom2.pk})

    def test_wizard_post_settings_step(self):
        """Test submitting settings advances to questions step."""
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
        self.assertEqual(wizard_data.get("step"), STEP_QUESTIONS)
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
        self.assertEqual(wizard_data.get("step"), STEP_REVIEW)
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
        self.assertEqual(response.context["review_subject"], "Science")
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