"""Tournament pages: permissions, state-aware actions, and what a screen may see.

These exercise the HTTP layer, which the service tests deliberately bypass. The
point of interest is that the *page* cannot be used to change an outcome: every
action is a POST, every POST is re-validated by the service layer, and an
interactive screen is refused everywhere.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from tournaments import services
from tournaments.models import (
    AdvancementStatus,
    Tournament,
    TournamentParticipantStatus,
    TournamentStatus,
)

from core.tests.utils import (
    TEST_PASSWORD,
    add_participants,
    create_administrator,
    create_classroom,
    create_teacher,
    create_tournament,
    played_competition,
)


class TournamentPageAccessTests(TestCase):
    """21. Unauthorised access to tournament operations."""

    def setUp(self) -> None:
        self.owner = create_teacher(username="owner")
        self.other = create_teacher(username="other")
        self.administrator = create_administrator()
        self.tournament = create_tournament(created_by=self.owner, name="Cup 2026")
        self.url = reverse("tournaments:detail", args=[self.tournament.pk])

    def test_an_anonymous_visitor_is_sent_to_sign_in(self) -> None:
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_an_anonymous_visitor_cannot_post_an_action(self) -> None:
        action = reverse("tournaments:action", args=[self.tournament.pk])

        response = self.client.post(action, {"action": "open"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.DRAFT)

    def test_another_teacher_gets_a_404_for_someone_elses_tournament(self) -> None:
        self.client.force_login(self.other)

        response = self.client.get(self.url)

        # A 404, not a 403: whether the tournament exists is not something an
        # unauthorised user is entitled to learn.
        self.assertEqual(response.status_code, 404)

    def test_another_teacher_cannot_post_an_action(self) -> None:
        self.client.force_login(self.other)

        response = self.client.post(
            reverse("tournaments:action", args=[self.tournament.pk]),
            {"action": "open"},
        )

        self.assertEqual(response.status_code, 404)
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.DRAFT)

    def test_an_administrator_may_manage_any_tournament(self) -> None:
        self.client.force_login(self.administrator)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cup 2026")

    def test_the_owning_teacher_sees_their_tournament(self) -> None:
        self.client.force_login(self.owner)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_the_list_shows_only_tournaments_the_viewer_owns(self) -> None:
        theirs = create_tournament(created_by=self.other, name="Not Yours")

        self.client.force_login(self.owner)
        response = self.client.get(reverse("tournaments:list"))

        self.assertContains(response, "Cup 2026")
        self.assertNotContains(response, "Not Yours")

    def test_the_hall_of_fame_requires_a_session(self) -> None:
        response = self.client.get(reverse("tournaments:hall_of_fame"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_a_screen_cannot_reach_the_tournament_pages(self) -> None:
        """A screen has a Screen ID, not credentials, so it gets nothing."""
        for name in ("tournaments:list", "tournaments:hall_of_fame", "reports:index"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))

                self.assertIn(response.status_code, (302, 404))

    def test_an_account_without_a_role_is_refused(self) -> None:
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(
            username="no.role", password=TEST_PASSWORD
        )
        user = get_user_model().objects.get(username="no.role")
        self.client.force_login(user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404)


class TournamentActionTests(TestCase):
    """Actions are state-aware and never take the outcome from the browser."""

    def setUp(self) -> None:
        self.owner = create_teacher(username="owner")
        self.tournament = create_tournament(created_by=self.owner, name="Cup 2026")
        self.action_url = reverse(
            "tournaments:action", args=[self.tournament.pk]
        )
        self.client.force_login(self.owner)

    def post(self, follow=False, **data):
        # `follow` is a client option, not form data: without pulling it out of
        # the arguments it would be posted as a field.
        return self.client.post(self.action_url, data, follow=follow)

    def test_opening_a_tournament_works_over_http(self) -> None:
        add_participants(self.tournament, "Science Lab A")

        self.post(action="open")

        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.OPEN)

    def test_opening_a_tournament_without_classrooms_is_refused_with_a_reason(self) -> None:
        response = self.post(action="open", follow=True)

        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.DRAFT)
        self.assertContains(response, "Add at least one classroom")

    def test_adding_a_classroom_over_http(self) -> None:
        classroom = create_classroom(name="Science Lab A")

        self.post(action="add_participant", classroom=classroom.pk)

        self.assertTrue(
            self.tournament.participants.filter(classroom=classroom).exists()
        )

    def test_adding_a_classroom_twice_is_refused(self) -> None:
        classroom = create_classroom(name="Science Lab A")
        self.post(action="add_participant", classroom=classroom.pk)

        response = self.post(
            action="add_participant", classroom=classroom.pk, follow=True
        )

        self.assertContains(response, "already taking part")
        self.assertEqual(self.tournament.participants.count(), 1)

    def test_creating_a_stage_over_http(self) -> None:
        self.post(action="create_stage", stage_type="qualification", advancing_count="2")

        stage = self.tournament.stages.get()
        self.assertEqual(stage.stage_type, "qualification")
        self.assertEqual(stage.advancing_count, 2)

    def test_a_blank_advancing_count_advances_everyone(self) -> None:
        self.post(action="create_stage", stage_type="final", advancing_count="")

        self.assertIsNone(self.tournament.stages.get().advancing_count)

    def test_an_unknown_stage_type_is_refused(self) -> None:
        response = self.post(action="create_stage", stage_type="play_off", follow=True)

        self.assertContains(response, "not a stage type")
        self.assertEqual(self.tournament.stages.count(), 0)

    def test_an_unsupported_action_is_refused(self) -> None:
        response = self.post(action="make_me_a_winner", follow=True)

        self.assertContains(response, "Unsupported action")
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.DRAFT)

    def test_finalizing_without_a_completed_stage_is_refused(self) -> None:
        add_participants(self.tournament, "Science Lab A")

        response = self.post(action="finalize", follow=True)

        self.assertContains(response, "Complete at least one stage")
        self.tournament.refresh_from_db()
        self.assertNotEqual(self.tournament.status, TournamentStatus.COMPLETED)

    def test_advancement_before_completion_is_refused_over_http(self) -> None:
        add_participants(self.tournament, "Science Lab A")
        stage = services.create_stage(self.tournament, "qualification", advancing_count=1)
        response = self.post(
            action="process_advancement", stage=stage.pk, follow=True
        )

        self.assertContains(response, "Complete this stage")
        self.assertFalse(hasattr(stage, "advancement"))

    def test_cancelling_a_tournament_over_http(self) -> None:
        self.post(action="cancel")

        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.CANCELLED)

    def test_a_cancelled_tournament_refuses_new_classrooms(self) -> None:
        self.post(action="cancel")
        classroom = create_classroom(name="Science Lab Z")

        response = self.post(
            action="add_participant", classroom=classroom.pk, follow=True
        )

        self.assertContains(response, "closed")
        self.assertEqual(self.tournament.participants.count(), 0)

    def test_a_stage_from_another_tournament_cannot_be_advanced(self) -> None:
        """The id in the form is checked against the tournament in the URL."""
        elsewhere = create_tournament(name="Other Cup")
        foreign_stage = services.create_stage(elsewhere, "qualification")

        response = self.post(
            action="complete_stage", stage=foreign_stage.pk, follow=True
        )

        self.assertEqual(response.status_code, 404)
        foreign_stage.refresh_from_db()
        self.assertNotEqual(foreign_stage.status, "completed")


class TieResolutionOverHttpTests(TestCase):
    """A tie is reported on the page and resolved by an administrator."""

    def setUp(self) -> None:
        self.owner = create_teacher(username="owner")
        self.administrator = create_administrator()
        self.tournament = create_tournament(created_by=self.owner, name="Tied Cup")
        participants = add_participants(
            self.tournament, "Science Lab A", "Science Lab B", "Science Lab C"
        )
        self.stage = services.create_stage(
            self.tournament, "qualification", advancing_count=2
        )
        competition = played_competition(
            scores={
                "Science Lab A": (300, 0),
                "Science Lab B": (250, 0),
                "Science Lab C": (250, 0),
            },
            classrooms=[p.classroom for p in participants],
        )
        services.attach_competition(self.stage, competition)
        services.complete_stage(self.stage)
        self.action_url = reverse(
            "tournaments:action", args=[self.tournament.pk]
        )

    def test_a_tie_is_written_and_shown_on_the_page(self) -> None:
        self.client.force_login(self.owner)
        self.client.post(
            self.action_url, {"action": "process_advancement", "stage": self.stage.pk}
        )

        self.stage.advancement.refresh_from_db()
        self.assertEqual(self.stage.advancement.status, AdvancementStatus.TIE)

        response = self.client.get(
            reverse("tournaments:detail", args=[self.tournament.pk])
        )
        self.assertContains(response, "A tie was found on the cut-off")
        self.assertContains(response, "Science Lab B")
        self.assertContains(response, "Science Lab C")

    def test_nobody_advances_while_the_tie_is_unresolved(self) -> None:
        self.client.force_login(self.owner)
        self.client.post(
            self.action_url, {"action": "process_advancement", "stage": self.stage.pk}
        )

        statuses = set(
            self.tournament.participants.values_list("status", flat=True)
        )
        self.assertEqual(statuses, {TournamentParticipantStatus.ENTERED})

    def test_a_teacher_cannot_resolve_a_tie(self) -> None:
        self.client.force_login(self.owner)
        self.client.post(
            self.action_url, {"action": "process_advancement", "stage": self.stage.pk}
        )
        chosen = self.tournament.participants.get(classroom__name="Science Lab A")

        response = self.client.post(
            self.action_url,
            {
                "action": "resolve_tie",
                "stage": self.stage.pk,
                "classroom_ids": [chosen.classroom_id, chosen.classroom_id],
            },
            follow=True,
        )

        self.assertContains(response, "Only an administrator may resolve a tie")
        statuses = set(self.tournament.participants.values_list("status", flat=True))
        self.assertEqual(statuses, {TournamentParticipantStatus.ENTERED})

    def test_an_administrator_can_resolve_a_tie(self) -> None:
        self.client.force_login(self.administrator)
        self.client.post(
            self.action_url, {"action": "process_advancement", "stage": self.stage.pk}
        )
        lab_a = self.tournament.participants.get(classroom__name="Science Lab A")
        lab_c = self.tournament.participants.get(classroom__name="Science Lab C")

        self.client.post(
            self.action_url,
            {
                "action": "resolve_tie",
                "stage": self.stage.pk,
                "classroom_ids": [lab_a.classroom_id, lab_c.classroom_id],
            },
        )

        statuses = {
            p.classroom.name: p.status
            for p in self.tournament.participants.select_related("classroom")
        }
        self.assertEqual(statuses["Science Lab C"], TournamentParticipantStatus.QUALIFIED)
        self.assertEqual(statuses["Science Lab B"], TournamentParticipantStatus.ELIMINATED)


class HallOfFameViewTests(TestCase):
    """17. The Hall of Fame shows real history and offers no editing."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="owner")
        self.tournament = create_tournament(
            created_by=self.teacher, name="Historic Cup", season="2025"
        )
        participants = add_participants(
            self.tournament, "Science Lab A", "Science Lab B"
        )
        stage = services.create_stage(self.tournament, "final")
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=[p.classroom for p in participants],
        )
        services.attach_competition(stage, competition)
        services.complete_stage(stage)
        services.process_advancement(stage)
        services.finalize_tournament(self.tournament)
        self.client.force_login(self.teacher)

    def test_the_hall_of_fame_lists_the_completed_tournament(self) -> None:
        response = self.client.get(reverse("tournaments:hall_of_fame"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Historic Cup")
        self.assertContains(response, "2025")
        self.assertContains(response, "Science Lab A")

    def test_the_hall_of_fame_offers_no_way_to_change_a_winner(self) -> None:
        response = self.client.get(reverse("tournaments:hall_of_fame"))

        body = response.content.decode().lower()
        # The only form on the page is the site's sign-out button in the header.
        # Nothing posts to a tournament action, and nothing carries an action
        # name, so this page cannot change a result even if someone edits the
        # HTML it renders.
        self.assertNotIn("/actions/", body)
        self.assertNotIn('name="action"', body)
        self.assertNotIn('name="classroom_ids"', body)

    def test_an_unfinished_tournament_is_absent(self) -> None:
        create_tournament(created_by=self.teacher, name="Still Running")

        response = self.client.get(reverse("tournaments:hall_of_fame"))

        self.assertNotContains(response, "Still Running")
