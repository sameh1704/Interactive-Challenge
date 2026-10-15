"""Regressions proved during final production acceptance (Phase 5)."""
from django.test import TestCase
from django.utils import translation
from core.tests.factories import create_teacher, create_administrator, create_classroom, create_question, create_competition
from competitions.models import Competition, CompetitionState
from live.services import start_competition, CompetitionControlError


class ProductionNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = create_teacher(username='p5.navigation.teacher')
        cls.admin = create_administrator(username='p5.navigation.admin')
        cls.room = create_classroom(name='Acceptance classroom')
        cls.room.teachers.add(cls.teacher)
        cls.competition = create_competition(teacher=cls.teacher, classrooms=[cls.room], questions=[create_question()])

    def test_teacher_dashboard_has_existing_question_and_competition_routes(self):
        self.client.force_login(self.teacher)
        response = self.client.get('/dashboard/')
        for path in ['/teacher/', '/teacher/questions/', '/teacher/questions/create/', '/teacher/competitions/', '/teacher/competitions/create/']:
            self.assertContains(response, 'href="'+path+'"')
        self.assertNotContains(response, 'href="/admin/"')
        self.assertNotContains(response, 'href="/deputy/"')

    def test_language_can_be_switched_on_login_and_core_dashboard(self):
        for authenticated in [False, True]:
            if authenticated:self.client.force_login(self.teacher)
            response = self.client.get('/dashboard/' if authenticated else '/accounts/login/')
            self.assertContains(response, 'action="/i18n/setlang/"')
            self.assertContains(response, 'name="language" value="ar"')

    def test_arabic_login_labels_are_translated(self):
        self.client.cookies['django_language']='ar'
        response=self.client.get('/accounts/login/')
        self.assertContains(response, 'اسم المستخدم')
        self.assertContains(response, 'كلمة المرور')
        self.assertContains(response, 'dir="rtl"')

    def test_arabic_core_navigation_and_reports(self):
        self.client.force_login(self.teacher)
        self.client.cookies['django_language']='ar'
        response=self.client.get('/dashboard/')
        self.assertContains(response,'بنك الأسئلة')
        self.assertNotContains(response,'>Dashboard</a>')
        response=self.client.get('/reports/')
        self.assertNotContains(response,'>Reports</h1>')
        self.assertContains(response,'التقارير')
        response=self.client.get(f'/reports/competitions/{self.competition.pk}/')
        self.assertNotContains(response,'>Participating classrooms</h2>')

    def test_administrator_metrics_follow_request_language(self):
        self.client.force_login(self.admin)
        self.client.cookies['django_language']='ar'
        response=self.client.get('/admin/')
        self.assertContains(response,'action="/i18n/setlang/"')
        self.assertNotEqual(str(response.context['metrics']['tiles'][0]['label']),'Classrooms')

    def test_finished_live_page_does_not_enable_start_again(self):
        from django.utils import timezone
        Competition.objects.filter(pk=self.competition.pk).update(
            state=CompetitionState.FINISHED, started_at=timezone.now(), finished_at=timezone.now())
        self.client.force_login(self.teacher)
        response=self.client.get(f'/live/competitions/{self.competition.pk}/')
        self.assertContains(response,'data-control="start_competition" data-requires="not_started"')
        self.assertNotContains(response,'data-requires="not_started,finished"')
        self.assertRegex(response.content.decode(), r'data-control="start_competition"[^>]*\bdisabled\b')


class CompetitionStartGuardTests(TestCase):
    def setUp(self):
        self.competition=create_competition(teacher=create_teacher(), classrooms=[create_classroom()], questions=[create_question()])

    def test_stale_second_start_does_not_reset_a_started_round(self):
        stale=Competition.objects.get(pk=self.competition.pk)
        first=start_competition(self.competition)
        with self.assertRaises(CompetitionControlError):
            start_competition(stale)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.started_at.isoformat(),first.payload['started_at'])

    def test_nonwaiting_round_cannot_be_started(self):
        for state in [CompetitionState.FINISHED, CompetitionState.QUESTION_ACTIVE]:
            with self.subTest(state=state):
                Competition.objects.filter(pk=self.competition.pk).update(state=state)
                self.competition.refresh_from_db()
                with self.assertRaises(CompetitionControlError):
                    start_competition(self.competition)
