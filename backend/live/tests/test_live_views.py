"""The two live pages, and what each is allowed to see.

The screen page is unauthenticated, because an interactive screen has no operator
and no credentials - its whole credential is the Screen ID in its URL. That makes
these tests mostly about what the screen is *not* given: no teacher controls, no
competition list, no other classrooms, no way to reach the teacher socket.

The teacher dashboard is the opposite: signed in, staff, and the owner of the
round. A 404 rather than a 403 for someone else's competition, so the endpoint
cannot be used to discover that one exists.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from core.tests.utils import (
    create_administrator,
    create_classroom,
    create_competition,
    create_question,
    create_screen,
    create_teacher,
)
from live.views import control_state_for


class LiveScreenPageTests(TestCase):
    """The page a classroom screen runs."""

    def setUp(self) -> None:
        self.teacher = create_teacher()
        self.lab = create_classroom(name="Science Lab A")
        self.other_lab = create_classroom(name="Science Lab B")
        self.question = create_question(
            text="What is 2 + 2?", options=["3", "4"], correct_option="4"
        )
        self.competition = create_competition(
            teacher=self.teacher,
            title="A versus B",
            classrooms=[self.lab, self.other_lab],
            questions=[self.question],
        )
        self.screen = create_screen(name="A board", classroom=self.lab)

    def _url(self, screen_id: str | None = None) -> str:
        return f"{reverse('live:screen')}?screen_id={screen_id or self.screen.screen_id}"

    def test_a_registered_screen_gets_its_page(self) -> None:
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "live/screen.html")

    def test_the_screen_id_is_accepted_in_any_case(self) -> None:
        # An operator types this by hand and a touchscreen keyboard may present
        # it in any case.
        response = self.client.get(self._url(self.screen.screen_id.lower()))

        self.assertEqual(response.status_code, 200)

    def test_the_page_names_the_screen_and_its_classroom(self) -> None:
        response = self.client.get(self._url())

        self.assertContains(response, self.screen.screen_id)
        self.assertContains(response, self.lab.display_name)

    def test_the_page_carries_no_administration_controls(self) -> None:
        response = self.client.get(self._url())
        body = response.content.decode()

        for control in (
            "start_competition",
            "end_question",
            "finish_competition",
            "advance",
            "start_question",
        ):
            self.assertNotIn(control, body)

    def test_the_page_carries_no_other_classroom(self) -> None:
        response = self.client.get(self._url())

        self.assertNotContains(response, self.other_lab.display_name)

    def test_the_page_exposes_no_answer_key(self) -> None:
        response = self.client.get(self._url())
        body = response.content.decode()

        self.assertNotIn("correct_answer", body)
        self.assertNotIn("correct_option", body)

    def test_the_page_points_only_at_the_screen_socket(self) -> None:
        response = self.client.get(self._url())

        self.assertContains(response, "/ws/live/screen/")
        self.assertNotContains(response, "/ws/live/teacher/")
        self.assertNotContains(response, "/ws/live/leaderboard/")

    def test_an_unknown_screen_id_is_a_404(self) -> None:
        response = self.client.get(self._url("AM-NOPE99"))

        self.assertEqual(response.status_code, 404)

    def test_a_retired_screen_is_a_404(self) -> None:
        # Same response as an unknown one, so the page cannot be used to
        # enumerate which Screen IDs exist.
        self.screen.active = False
        self.screen.save(update_fields=["active"])

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 404)

    def test_a_missing_screen_id_is_a_404(self) -> None:
        response = self.client.get(reverse("live:screen"))

        self.assertEqual(response.status_code, 404)


class TeacherDashboardTests(TestCase):
    """The teacher's control page for one round."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="owner")
        self.intruder = create_teacher(username="intruder")
        self.administrator = create_administrator(username="boss")
        self.lab = create_classroom(name="Science Lab A")
        self.question = create_question(
            text="What is 2 + 2?", options=["3", "4"], correct_option="4"
        )
        self.competition = create_competition(
            teacher=self.teacher,
            title="A versus nothing",
            classrooms=[self.lab],
            questions=[self.question],
        )

    def _url(self, competition=None) -> str:
        target = competition or self.competition
        return reverse(
            "live:teacher_dashboard", kwargs={"competition_id": target.pk}
        )

    def _sign_in(self, user, password=None) -> None:
        from core.tests.utils import TEST_PASSWORD

        self.assertTrue(
            self.client.login(username=user.username, password=password or TEST_PASSWORD)
        )

    def test_an_anonymous_visitor_is_sent_to_sign_in(self) -> None:
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_the_owner_sees_the_dashboard(self) -> None:
        self._sign_in(self.teacher)
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "live/teacher.html")
        self.assertContains(response, self.competition.title)

    def test_the_owner_sees_the_end_question_control(self) -> None:
        self._sign_in(self.teacher)
        response = self.client.get(self._url())

        self.assertContains(response, "End question")
        self.assertContains(response, 'data-control="end_question"')

    def test_every_control_is_declared_once(self) -> None:
        """No duplicated or conflicting controls.

        Two buttons that both mean "move on" is how a teacher ends up pressing
        the wrong one during a lesson, so the page must not offer a second route
        to the same transition.
        """
        self._sign_in(self.teacher)
        response = self.client.get(self._url())
        body = response.content.decode()

        for control in (
            "start_competition",
            "start_question",
            "end_question",
            "advance",
            "finish_competition",
        ):
            self.assertEqual(
                body.count(f'data-control="{control}"'),
                1,
                f"{control} should appear exactly once",
            )

    def test_another_teacher_gets_a_404(self) -> None:
        self._sign_in(self.intruder)
        response = self.client.get(self._url())

        # A 404, not a 403: whether a competition exists is not something an
        # unauthorised user is entitled to learn.
        self.assertEqual(response.status_code, 404)

    def test_an_administrator_may_take_over(self) -> None:
        self._sign_in(self.administrator)
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)

    def test_an_unknown_competition_is_a_404(self) -> None:
        self._sign_in(self.teacher)
        response = self.client.get(
            reverse("live:teacher_dashboard", kwargs={"competition_id": 99999})
        )

        self.assertEqual(response.status_code, 404)

    def test_the_dashboard_points_at_the_teacher_socket(self) -> None:
        self._sign_in(self.teacher)
        response = self.client.get(self._url())

        self.assertContains(response, f"/ws/live/teacher/{self.competition.pk}/")

    def test_the_dashboard_lists_the_participating_classrooms(self) -> None:
        self._sign_in(self.teacher)
        response = self.client.get(self._url())

        self.assertContains(response, self.lab.display_name)


class ControlStateTests(TestCase):
    """The distinction the dashboard's buttons depend on.

    ``Competition.state`` is ``waiting`` both before a round has started and
    between its questions. The controls must tell those apart, or "Start
    competition" would either be permanently available or permanently hidden.
    """

    def setUp(self) -> None:
        self.teacher = create_teacher()
        self.lab = create_classroom()
        self.competition = create_competition(
            teacher=self.teacher, classrooms=[self.lab]
        )

    def test_an_unstarted_round_reports_not_started(self) -> None:
        self.assertEqual(control_state_for(self.competition), "not_started")

    def test_a_started_but_idle_round_reports_waiting(self) -> None:
        from live import services

        services.start_competition(self.competition)
        self.competition.refresh_from_db()

        self.assertEqual(control_state_for(self.competition), "waiting")

    def test_an_open_question_reports_question_active(self) -> None:
        from live import services

        services.start_competition(self.competition)
        services.start_question(self.competition, position=1)
        self.competition.refresh_from_db()

        self.assertEqual(control_state_for(self.competition), "question_active")

    def test_the_unstarted_round_only_offers_start_competition(self) -> None:
        from core.tests.utils import TEST_PASSWORD

        self.client.login(username=self.teacher.username, password=TEST_PASSWORD)
        response = self.client.get(
            reverse(
                "live:teacher_dashboard", kwargs={"competition_id": self.competition.pk}
            )
        )

        self.assertContains(response, 'data-state="not_started"')