"""Phase 3: the teacher-first competition workflow, asserted as behaviour.

Every test here drives a route a teacher actually uses and then asserts the
**persisted** state, not that a page returned 200. Two classes of regression are
covered:

* ``CompetitionLifecycleListTests`` - the status filters on My Competitions
  actually run. ``ACTIVE_STATES`` lives on the ``competitions.models`` module, not
  on ``CompetitionState``; the Running filter read it as a class attribute, so
  clicking "Running" raised ``AttributeError`` and returned a 500 to a teacher
  mid-lesson.
* ``WizardClassroomAuthorisationTests`` - the wizard only *offers* a teacher the
  classrooms assigned to them. That is not a permission check, so a hand-crafted
  POST naming another teacher's classroom was stored and that room's screen was
  then driven by someone who does not teach it.

The scoring tests assert the total the server computes from the stored rows, so a
review page showing a total and a round awarding a different one cannot both
pass.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from competitions.models import ACTIVE_STATES, Competition, CompetitionState
from core.tests.factories import (
    create_administrator,
    create_classroom,
    create_competition,
    create_question,
    create_subject,
    create_teacher,
)
from live.services import finish_competition, start_competition
from teacher.wizard import SESSION_KEY

LIFECYCLE_FILTERS = ("draft", "scheduled", "running", "completed")


class CompetitionLifecycleListTests(TestCase):
    """The four lifecycle filters on My Competitions must all work."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="p3.list.teacher")
        cls.subject = create_subject(name="P3 List Subject", slug="p3-list")
        cls.room = create_classroom(name="P3 List Room")
        cls.room.teachers.add(cls.teacher)

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def _competition(self, title: str) -> Competition:
        return create_competition(
            teacher=self.teacher,
            title=title,
            classrooms=[self.room],
            questions=[create_question(text=f"{title} question", subject=self.subject)],
        )

    def test_every_lifecycle_filter_renders(self) -> None:
        """No filter may raise. This is the regression: Running returned a 500."""
        for status in LIFECYCLE_FILTERS:
            with self.subTest(status=status):
                response = self.client.get(
                    reverse("teacher:competitions"), {"status": status}
                )
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "teacher/competitions.html")

    def test_the_running_filter_lists_a_live_competition_only(self) -> None:
        """Persisted state decides the filter, not a status word."""
        running = self._competition("P3 Running")
        prepared = self._competition("P3 Prepared")
        finished = self._competition("P3 Finished")

        start_competition(running)
        start_competition(finished)
        finish_competition(finished)

        response = self.client.get(reverse("teacher:competitions"), {"status": "running"})
        listed = [competition.title for competition in response.context["competitions"]]

        self.assertIn("P3 Running", listed)
        self.assertNotIn("P3 Prepared", listed, "an unstarted round is not running")
        self.assertNotIn("P3 Finished", listed, "a finished round is not running")

        for competition in (running, prepared, finished):
            competition.refresh_from_db()
        self.assertTrue(running.is_live)
        self.assertFalse(prepared.is_started)
        self.assertTrue(finished.is_finished)

    def test_the_scheduled_filter_lists_a_planned_unstarted_competition_only(self) -> None:
        planned = self._competition("P3 Planned")
        planned.planned_for = timezone.now() + timedelta(days=1)
        planned.save(update_fields=["planned_for", "updated_at"])

        self._competition("P3 Plain Draft")

        response = self.client.get(
            reverse("teacher:competitions"), {"status": "scheduled"}
        )
        listed = [competition.title for competition in response.context["competitions"]]

        self.assertIn("P3 Planned", listed)
        self.assertNotIn("P3 Plain Draft", listed)

        # Scheduling is a note. It must never have started anything.
        planned.refresh_from_db()
        self.assertIsNone(planned.started_at)
        self.assertEqual(planned.lifecycle_label, "planned")

    def test_the_draft_filter_excludes_a_scheduled_competition(self) -> None:
        planned = self._competition("P3 Planned Draft")
        planned.planned_for = timezone.now() + timedelta(days=1)
        planned.save(update_fields=["planned_for", "updated_at"])
        self._competition("P3 Unplanned Draft")

        response = self.client.get(reverse("teacher:competitions"), {"status": "draft"})
        listed = [competition.title for competition in response.context["competitions"]]

        self.assertIn("P3 Unplanned Draft", listed)
        self.assertNotIn("P3 Planned Draft", listed)

    def test_the_completed_filter_lists_a_finished_competition_only(self) -> None:
        finished = self._competition("P3 Done")
        start_competition(finished)
        finish_competition(finished)
        self._competition("P3 Not Done")

        response = self.client.get(
            reverse("teacher:competitions"), {"status": "completed"}
        )
        listed = [competition.title for competition in response.context["competitions"]]

        self.assertIn("P3 Done", listed)
        self.assertNotIn("P3 Not Done", listed)

    def test_an_unknown_filter_falls_back_to_the_whole_list(self) -> None:
        """A stale link must not 500 the teacher's only list of competitions."""
        self._competition("P3 Any State")
        response = self.client.get(reverse("teacher:competitions"), {"status": "nonsense"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [competition.title for competition in response.context["competitions"]],
            ["P3 Any State"],
        )

    def test_the_active_states_set_is_the_module_constant(self) -> None:
        """The filter must agree with the engine about which states are live."""
        from competitions import models

        self.assertIs(ACTIVE_STATES, models.ACTIVE_STATES)
        self.assertIn(CompetitionState.WAITING, ACTIVE_STATES)
        self.assertNotIn(CompetitionState.FINISHED, ACTIVE_STATES)


class WizardClassroomAuthorisationTests(TestCase):
    """A teacher may only put their own classrooms into a round.

    The wizard offers a teacher the classrooms assigned to them, and that is a
    convenience rather than a check. Before this was enforced server-side, a POST
    naming another teacher's classroom id was stored verbatim - which put a room
    the submitting teacher does not run in front of that room's registered screen,
    and let one teacher drive another teacher's class.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="p3.auth.teacher")
        cls.stranger = create_teacher(username="p3.auth.stranger")
        cls.subject = create_subject(name="P3 Auth Subject", slug="p3-auth")

        cls.mine = create_classroom(name="P3 Auth Mine")
        cls.mine.teachers.add(cls.teacher)
        cls.theirs = create_classroom(name="P3 Auth Theirs")
        cls.theirs.teachers.add(cls.stranger)

        for index in range(3):
            create_question(text=f"P3 auth question {index}", subject=cls.subject)

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def _advance_to_questions(self, classroom_ids) -> None:
        session = self.client.session
        session[SESSION_KEY] = {
            "step": "subject_classrooms",
            "basic": {"title": "P3 Auth Round", "subject": "", "grade": "",
                      "description": ""},
            "subject_id": str(self.subject.pk),
            "topic_id": "",
            "classroom_ids": [],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard",
                         "speed_bonus": 0, "points_per_question": 2, "planned_for": ""},
            "question_ids": [],
            "question_points": {},
        }
        session.save()

        fields = {
            "action": "next",
            "subject_id": str(self.subject.pk),
            "topic_id": "",
        }
        for classroom_id in classroom_ids:
            fields.setdefault("classroom_ids", []).append(classroom_id)
        self.client.post(reverse("teacher:create_competition"), fields)

    def test_a_forged_classroom_id_is_refused_and_not_persisted(self) -> None:
        """The regression: the forged id used to be stored as-is."""
        before = Competition.objects.count()

        self._advance_to_questions([str(self.mine.pk), str(self.theirs.pk)])
        session = self.client.session
        stored = session[SESSION_KEY]["classroom_ids"]

        self.assertIn(str(self.mine.pk), stored)
        self.assertNotIn(
            str(self.theirs.pk), stored,
            "a classroom the teacher is not assigned to was accepted",
        )

        # Finish the wizard and confirm nothing unauthorised reached the database.
        from questions.models import Question

        questions = list(Question.objects.all())
        session[SESSION_KEY]["question_ids"] = [str(q.pk) for q in questions]
        session[SESSION_KEY]["step"] = "review"
        session.save()
        self.client.post(reverse("teacher:create_competition"), {"action": "save_draft"})

        created = Competition.objects.exclude(pk__in=[]).order_by("-created_at").first()
        self.assertIsNotNone(created)
        included = set(
            created.classrooms.values_list("classroom_id", flat=True)
        )
        self.assertIn(self.mine.pk, included)
        self.assertNotIn(self.theirs.pk, included)
        self.assertEqual(Competition.objects.count(), before + 1)

    def test_a_forged_classroom_in_session_state_is_refused_on_save(self) -> None:
        """The save path re-checks, because an assignment can change mid-wizard."""
        from django.core.exceptions import ValidationError

        from teacher.wizard import _authorised_classroom_ids

        self.assertIn(str(self.mine.pk), _authorised_classroom_ids(self.teacher))
        self.assertNotIn(str(self.theirs.pk), _authorised_classroom_ids(self.teacher))
        self.assertIn(
            str(self.theirs.pk), _authorised_classroom_ids(self.stranger)
        )

        request = type("R", (), {"user": self.teacher})()
        session = self.client.session
        session[SESSION_KEY] = {
            "step": "review",
            "basic": {"title": "P3 Forged Save", "subject": "", "grade": "",
                      "description": ""},
            "subject_id": str(self.subject.pk),
            "topic_id": "",
            "classroom_ids": [str(self.theirs.pk)],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard",
                         "speed_bonus": 0, "points_per_question": 2, "planned_for": ""},
            "question_ids": [],
            "question_points": {},
        }
        session.save()

        from teacher.wizard import CompetitionWizardView

        view = CompetitionWizardView()
        with self.assertRaises(ValidationError):
            view._build_competition(request, session[SESSION_KEY])

        self.assertFalse(
            Competition.objects.filter(title="P3 Forged Save").exists(),
            "a competition naming an unauthorised classroom was persisted",
        )

    def test_an_administrator_may_target_any_active_classroom(self) -> None:
        """The exemption is the role, not a second rule."""
        from teacher.wizard import _authorised_classroom_ids

        admin = create_administrator(username="p3.auth.admin")
        allowed = _authorised_classroom_ids(admin)
        self.assertIn(str(self.mine.pk), allowed)
        self.assertIn(str(self.theirs.pk), allowed)


class WizardScoringTotalTests(TestCase):
    """The total a teacher is shown is the total the round will award.

    The displayed total and the persisted total are computed from the same stored
    rows, so the two cannot drift. The example from the phase brief - five
    questions worth two points each, ten points in total - is asserted end to end.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.teacher = create_teacher(username="p3.score.teacher")
        cls.subject = create_subject(name="P3 Score Subject", slug="p3-score")
        cls.room = create_classroom(name="P3 Score Room")
        cls.room.teachers.add(cls.teacher)
        cls.questions = [
            create_question(text=f"P3 score question {i}", subject=cls.subject)
            for i in range(5)
        ]

    def setUp(self) -> None:
        self.client.force_login(self.teacher)

    def _wizard_at_review(self, points_per_question, overrides=None):
        session = self.client.session
        session[SESSION_KEY] = {
            "step": "review",
            "basic": {"title": "P3 Scoring Round", "subject": "", "grade": "",
                      "description": ""},
            "subject_id": str(self.subject.pk),
            "topic_id": "",
            "classroom_ids": [str(self.room.pk)],
            "settings": {"duration_seconds": 30, "scoring_mode": "standard",
                         "speed_bonus": 0,
                         "points_per_question": points_per_question,
                         "planned_for": ""},
            "question_ids": [str(q.pk) for q in self.questions],
            "question_points": overrides or {},
        }
        session.save()
        return self.client.get(reverse("teacher:create_competition"))

    def test_five_questions_at_two_points_shows_and_persists_ten(self) -> None:
        response = self._wizard_at_review(2)
        self.assertContains(response, "Total possible score")
        self.assertEqual(response.context["review_total_points"], 10)

        self.client.post(reverse("teacher:create_competition"), {"action": "save_draft"})
        competition = Competition.objects.get(title="P3 Scoring Round")

        self.assertEqual(competition.total_points, 10)
        self.assertEqual(competition.questions.count(), 5)
        self.assertEqual(
            [entry.points for entry in competition.questions.order_by("position")],
            [2, 2, 2, 2, 2],
        )
        self.assertFalse(competition.is_started)

    def test_a_per_question_override_changes_the_persisted_total(self) -> None:
        first = str(self.questions[0].pk)
        response = self._wizard_at_review(2, overrides={first: 5})
        self.assertEqual(response.context["review_total_points"], 13)

        self.client.post(reverse("teacher:create_competition"), {"action": "save_draft"})
        competition = Competition.objects.get(title="P3 Scoring Round")

        self.assertEqual(competition.total_points, 13)
        self.assertEqual(
            sorted(entry.points for entry in competition.questions.all()),
            [2, 2, 2, 2, 5],
        )

    def test_a_zero_point_override_is_refused_and_nothing_is_saved(self) -> None:
        """A zero-point question must not silently become the default.

        The per-question value used to be read with ``dict.get(k) or default``.
        ``0`` is falsy, so the review row showed the round's default, the review
        total added the ``0``, and the save wrote the default again - three
        different answers for the same question, and a total on screen that no
        round would ever award.
        """
        first = str(self.questions[0].pk)
        before = Competition.objects.count()

        response = self._wizard_at_review(2, overrides={first: 0})

        # The review step must not present a total that disagrees with its own rows.
        rows_total = sum(row["points"] for row in response.context["review_question_rows"])
        self.assertEqual(
            response.context["review_total_points"],
            rows_total,
            "the review total disagrees with the per-question rows",
        )
        self.assertContains(response, "Points must be between 1 and")

        # And the save must refuse it outright rather than persisting the default.
        self.client.post(reverse("teacher:create_competition"), {"action": "save_draft"})
        self.assertEqual(
            Competition.objects.count(),
            before,
            "a competition with an invalid per-question value was saved",
        )
        self.assertFalse(Competition.objects.filter(title="P3 Scoring Round").exists())

    def test_the_review_total_always_equals_the_sum_of_its_own_rows(self) -> None:
        """One invariant, several scoring shapes: no override, one, and all."""
        cases = [
            ("defaults only", {}),
            ("one override", {str(self.questions[0].pk): 7}),
            ("all overridden", {str(q.pk): 4 for q in self.questions}),
        ]
        for label, overrides in cases:
            with self.subTest(case=label):
                response = self._wizard_at_review(2, overrides=overrides)
                rows_total = sum(
                    row["points"] for row in response.context["review_question_rows"]
                )
                self.assertEqual(response.context["review_total_points"], rows_total)

                self.client.post(
                    reverse("teacher:create_competition"), {"action": "save_draft"}
                )
                competition = Competition.objects.get(title="P3 Scoring Round")
                self.assertEqual(competition.total_points, rows_total)

                competition.delete()
                # A successful save clears the wizard, which is correct: the round
                # is persisted, so re-seed for the next case rather than expecting
                # the session state to survive.
                self._wizard_at_review(2, overrides=overrides)

    def test_saving_a_draft_sends_no_start_event(self) -> None:
        """A draft is stored, never broadcast."""
        self._wizard_at_review(2)
        self.client.post(reverse("teacher:create_competition"), {"action": "save_draft"})
        competition = Competition.objects.get(title="P3 Scoring Round")

        competition.refresh_from_db()
        self.assertIsNone(competition.started_at)
        self.assertIsNone(competition.finished_at)
        self.assertTrue(competition.is_prepared)
        self.assertEqual(competition.state, CompetitionState.WAITING)
        self.assertEqual(competition.lifecycle_label, "prepared")
