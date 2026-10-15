from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from classrooms.models import Classroom
from competitions.models import Competition, CompetitionState
from core.tests.factories import default_subject
from questions.models import Question, QuestionType
from tournaments.models import Tournament, TournamentStageType
from teacher.tournament_wizard import (
    COMPETITION_RETURN_KEY,
    SESSION_KEY,
    STEP_ADVANCEMENT,
    STEP_COMPETITIONS,
    STEP_CREATE,
    STEP_PARTICIPANTS,
    STEP_REVIEW,
    STEP_STAGES,
)
from teacher.wizard import SESSION_KEY as COMPETITION_SESSION_KEY, STEP_REVIEW as COMPETITION_REVIEW

User = get_user_model()


class TournamentWizardTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="ui4_teacher",
            email="ui4_teacher@example.test",
            password="ui4-test-password",
            role="teacher",
        )
        self.classrooms = []
        for number in range(1, 5):
            classroom = Classroom.objects.create(
                name=f"UI4 Test Classroom {number}",
                grade="Grade 7",
                building=f"UI4 Test Building {number}",
                active=True,
            )
            classroom.teachers.add(self.teacher)
            self.classrooms.append(classroom)
        self.outside_classroom = Classroom.objects.create(
            name="UI4 Test Unassigned Classroom",
            active=True,
        )
        self.client.force_login(self.teacher)
        self.url = reverse("teacher:create_tournament")

    def _data(self, step=STEP_CREATE, **overrides):
        data = {
            "step": step,
            "tournament": {
                "name": "UI4 Test Championship",
                "subject": "Science",
                "grade": "Grade 7",
                "season": "UI4 Test Season",
                "description": "UI4 wizard test",
            },
            "classroom_ids": [str(classroom.pk) for classroom in self.classrooms],
            "stage_template": "qualification_final",
            "advancing_count": "2",
            "semi_advancing_count": "2",
            "competition_ids": {
                "qualification": [],
                "semi_final": [],
                "final": [],
            },
        }
        data.update(overrides)
        return data

    def _save_wizard(self, data):
        session = self.client.session
        session[SESSION_KEY] = data
        session.save()

    def test_wizard_shows_seven_steps_and_classroom_details(self):
        self._save_wizard(self._data(STEP_PARTICIPANTS))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["wizard_steps"]), 7)
        self.assertContains(response, "UI4 Test Building 1")
        self.assertContains(response, "previous tournament")

    def test_unassigned_classroom_is_rejected(self):
        self._save_wizard(self._data(STEP_PARTICIPANTS))

        response = self.client.post(
            self.url,
            {
                "action": "next",
                "classroom_ids": [
                    str(self.classrooms[0].pk),
                    str(self.outside_classroom.pk),
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "no longer available",
            status_code=400,
        )
        self.assertEqual(self.client.session[SESSION_KEY]["step"], STEP_PARTICIPANTS)

    def test_advancement_requires_top_count_below_participant_count(self):
        data = self._data(STEP_ADVANCEMENT, advancing_count="4")
        self._save_wizard(data)

        response = self.client.post(
            self.url,
            {"action": "next", "advancing_count": "4", "semi_advancing_count": "2"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "Choose between 1 and 3 classrooms.",
            status_code=400,
        )

    def test_create_uses_tournament_services_to_make_four_classroom_bracket(self):
        self._save_wizard(self._data())

        response = self.client.post(self.url, {"action": "create"})

        tournament = Tournament.objects.get(name="UI4 Test Championship")
        self.assertRedirects(
            response,
            reverse("teacher:tournament_detail", args=[tournament.pk]),
        )
        self.assertEqual(tournament.created_by, self.teacher)
        self.assertEqual(tournament.participants.count(), 4)
        stages = list(tournament.stages.order_by("order"))
        self.assertEqual(
            [stage.stage_type for stage in stages],
            [TournamentStageType.QUALIFICATION, TournamentStageType.FINAL],
        )
        self.assertEqual(stages[0].advancing_count, 2)
        self.assertIsNone(stages[1].advancing_count)
        self.assertEqual(self.client.session.get(SESSION_KEY), None)

        detail = self.client.get(
            reverse("teacher:tournament_detail", args=[tournament.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "UI4 Test Building 1")
        self.assertContains(detail, "Qualification")

    def test_semi_final_template_creates_three_stages_and_rules(self):
        data = self._data(
            STEP_CREATE,
            stage_template="qualification_semi_final",
            advancing_count="3",
            semi_advancing_count="2",
        )
        self._save_wizard(data)

        response = self.client.post(self.url, {"action": "create"})

        self.assertEqual(response.status_code, 302)
        tournament = Tournament.objects.get(name="UI4 Test Championship")
        stages = list(tournament.stages.order_by("order"))
        self.assertEqual(
            [stage.stage_type for stage in stages],
            [
                TournamentStageType.QUALIFICATION,
                TournamentStageType.SEMI_FINAL,
                TournamentStageType.FINAL,
            ],
        )
        self.assertEqual(stages[0].advancing_count, 3)
        self.assertEqual(stages[1].advancing_count, 2)
        self.assertIsNone(stages[2].advancing_count)

    def test_competition_wizard_handoff_prefills_and_returns_selected_competition(self):
        self._save_wizard(self._data(STEP_COMPETITIONS))

        response = self.client.get(
            self.url + "?create_competition=qualification"
        )

        self.assertRedirects(response, reverse("teacher:create_competition"))
        session = self.client.session
        self.assertEqual(session[COMPETITION_RETURN_KEY], "qualification")
        competition_data = session[COMPETITION_SESSION_KEY]
        self.assertEqual(competition_data["basic"]["title"], "UI4 Test Championship — Qualification")
        self.assertEqual(
            set(competition_data["classroom_ids"]),
            {str(classroom.pk) for classroom in self.classrooms},
        )
        next_step = self.client.post(
            response.url,
            {
                "action": "next",
                **competition_data["basic"],
            },
        )
        self.assertRedirects(next_step, response.url)
        competition_page = self.client.get(next_step.url)
        self.assertEqual(competition_page.status_code, 200)
        self.assertEqual(
            competition_page.context["selected_classroom_ids"],
            {classroom.pk for classroom in self.classrooms},
        )

        question = Question.objects.create(
            text="UI4 Test question",
            subject=default_subject(),
            question_type=QuestionType.MULTIPLE_CHOICE,
            options=["A", "B"],
            correct_option="A",
        )
        competition_data.update(
            {
                "step": COMPETITION_REVIEW,
                "question_ids": [str(question.pk)],
            }
        )
        session[COMPETITION_SESSION_KEY] = competition_data
        session.save()

        response = self.client.post(
            reverse("teacher:create_competition"),
            {"action": "submit"},
        )

        competition = Competition.objects.get(title="UI4 Test Championship — Qualification")
        self.assertRedirects(
            response,
            f"{self.url}?competition_id={competition.pk}&stage=qualification",
        )
        self.assertEqual(competition.state, CompetitionState.WAITING)
        self.assertEqual(
            set(competition.classrooms.values_list("classroom_id", flat=True)),
            {classroom.pk for classroom in self.classrooms},
        )

        response = self.client.get(response.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, competition.title)
        self.assertEqual(
            self.client.session[SESSION_KEY]["competition_ids"]["qualification"],
            [str(competition.pk)],
        )
        self.assertNotIn(COMPETITION_RETURN_KEY, self.client.session)

    def test_competition_cancel_returns_to_tournament_wizard(self):
        session = self.client.session
        session[COMPETITION_RETURN_KEY] = "qualification"
        session[COMPETITION_SESSION_KEY] = {
            "step": "basic",
            "basic": {"title": "", "subject": "", "grade": "", "description": ""},
            "classroom_ids": [],
            "settings": {},
            "question_ids": [],
        }
        session.save()

        response = self.client.post(
            reverse("teacher:create_competition"),
            {"action": "cancel"},
        )

        self.assertRedirects(response, self.url)
        self.assertNotIn(COMPETITION_RETURN_KEY, self.client.session)

    def test_admin_lists_tournaments_created_by_other_teachers(self):
        other = User.objects.create_user(
            username="ui4_other_teacher",
            password="ui4-test-password",
            role="teacher",
        )
        tournament = Tournament.objects.create(
            name="UI4 Test Other Teacher Championship",
            created_by=other,
        )
        admin = User.objects.create_user(
            username="ui4_admin",
            password="ui4-test-password",
            role="administrator",
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("teacher:tournaments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, tournament.name)
