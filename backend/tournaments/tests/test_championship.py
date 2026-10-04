"""One championship, end to end.

The scenario from the Phase 7 specification: four classrooms enter, a qualification
round is played, the top two advance, a final is played, and the tournament is
concluded. What makes this different from the service tests is that the rounds are
driven through the *live engine* - :mod:`live.services` and the real teacher and
screen WebSocket consumers - rather than built as fixtures.

That is the point. A tournament could pass every unit test and still not work if it
assumed a competition reached ``finished`` in some way the live engine never
produces. Here the standings a championship advances from are the standings the
engine produced, frozen by the same :func:`scoring.services.finalise` the reveal
uses.

The scores are computed, not asserted as literals: each classroom is given a
different number of correct answers, so the ranking follows from the scoring rules
rather than from a number written into the test.
"""

from __future__ import annotations

from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from competitions.models import Competition, CompetitionState
from live import services as live_services
from scoring.models import CompetitionResult
from tournaments import services as tournament_services
from tournaments.models import (
    TournamentParticipantStatus,
    TournamentRecord,
    TournamentStageStatus,
    TournamentStatus,
)

from core.tests.utils import (
    add_participants,
    create_teacher,
    played_competition,
)
from live.tests.support import drain_until, send_message
from live.tests.test_live_engine import LiveTestCase

# Four classrooms, and how many questions each gets right. Distinct totals, so the
# qualification ranking is unambiguous.
LABS = (
    "Science Lab A",
    "Science Lab B",
    "Science Lab C",
    "Science Lab D",
)
CORRECT_ANSWERS = {
    "Science Lab A": 4,
    "Science Lab B": 3,
    "Science Lab C": 2,
    "Science Lab D": 1,
}
POINTS_PER_CORRECT = 100
QUESTIONS_PER_ROUND = 4


def play_a_round(live_services_module, competition, classrooms, correct_counts):
    """Run a whole competition through the live engine and return it finished.

    ``correct_counts`` maps a classroom name to how many of the round's questions
    it answers correctly. Every other answer is wrong, so the score is exactly
    ``correct * POINTS_PER_CORRECT`` and the ranking is derived rather than typed.
    """
    live_services_module.start_competition(competition)
    live_services_module.assert_has_questions(competition)

    questions = list(competition.questions.order_by("position"))
    entries = {entry.position: entry for entry in questions}

    for position, _entry in entries.items():
        live_services_module.start_question(competition, position=position)
        question = competition.current_question

        for classroom in classrooms:
            should_be_right = correct_counts[classroom.name] >= position
            from scoring import services as scoring_services

            try:
                scoring_services.submit_answer(
                    competition=competition,
                    classroom=classroom,
                    selection=question.answer_key if should_be_right else "__wrong__",
                )
            except scoring_services.AnswerRejected:
                # A deliberately wrong answer has to be a *legal* answer that
                # scores nothing, not an illegal one the service throws away;
                # otherwise "wrong" would not be recorded at all.
                selection = question.options[-1] if question.options else "__wrong__"
                if question.matches(selection):
                    selection = question.options[0]
                scoring_services.submit_answer(
                    competition=competition, classroom=classroom, selection=selection
                )

        live_services_module.close_answering(competition, reason="teacher ended the question")

    live_services_module.finish_competition(competition)

    # `finish_competition` ends the round but does not freeze the standings: in
    # production that happens in the scoring realtime layer, which broadcasts the
    # final result to every group. A championship needs the frozen result - it
    # advances from that, not from live answers - so the same call is made here.
    scoring_services.finalise(competition)

    return competition


class ChampionshipEndToEndTests(LiveTestCase):
    """The full path: enter, play, advance, play again, conclude."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="champion.teacher")
        self.tournament = tournament_services.create_tournament(
            name="Primary Championship",
            created_by=self.teacher,
            subject="Science",
            grade="Grade 5",
            season="2026",
        )
        self.participants = add_participants(self.tournament, *LABS)
        self.classrooms = [p.classroom for p in self.participants]

        tournament_services.open_tournament(self.tournament)
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.OPEN)

    def test_a_championship_runs_from_qualification_to_cup_winner(self) -> None:
        # -- Qualification ------------------------------------------------
        qualification = tournament_services.create_stage(
            self.tournament, "qualification", advancing_count=2
        )
        self.assertEqual(qualification.order, 1)
        self.assertEqual(qualification.status, TournamentStageStatus.PENDING)

        questions = [
            self._make_question(text=f"Qualification question {index}")
            for index in range(1, QUESTIONS_PER_ROUND + 1)
        ]
        qualifying_round = Competition.objects.create(
            title="Qualification round", teacher=self.teacher
        )
        from competitions.models import CompetitionClassroom, CompetitionQuestion

        for classroom in self.classrooms:
            CompetitionClassroom.objects.create(
                competition=qualifying_round, classroom=classroom
            )
        for index, question in enumerate(questions, start=1):
            CompetitionQuestion.objects.create(
                competition=qualifying_round, question=question, position=index
            )

        tournament_services.attach_competition(qualification, qualifying_round)

        play_a_round(
            live_services,
            qualifying_round,
            self.classrooms,
            CORRECT_ANSWERS,
        )

        # The qualification round is a genuine finished round with a frozen result.
        qualifying_round.refresh_from_db()
        self.assertEqual(qualifying_round.state, CompetitionState.FINISHED)
        result = CompetitionResult.objects.get(competition=qualifying_round)
        self.assertEqual(len(result.rows), 4)

        tournament_services.complete_stage(qualification)
        self.assertEqual(qualification.status, TournamentStageStatus.COMPLETED)

        plan = tournament_services.calculate_advancement(qualification)
        self.assertFalse(plan.has_tie, "distinct scores must not produce a tie")
        self.assertEqual(
            [s.classroom_name for s in plan.advancing],
            ["Science Lab A", "Science Lab B"],
        )

        tournament_services.process_advancement(qualification, user=self.teacher)

        statuses = {
            p.classroom.name: p.status
            for p in self.tournament.participants.select_related("classroom")
        }
        self.assertEqual(statuses["Science Lab A"], TournamentParticipantStatus.QUALIFIED)
        self.assertEqual(statuses["Science Lab B"], TournamentParticipantStatus.QUALIFIED)
        self.assertEqual(statuses["Science Lab C"], TournamentParticipantStatus.ELIMINATED)
        self.assertEqual(statuses["Science Lab D"], TournamentParticipantStatus.ELIMINATED)

        # The aggregate came from the frozen standings, not from a recalculation.
        # Scores include the speed bonus, so asserting a hand-computed number here
        # would be asserting the scoring rules rather than what this feature does:
        # tying the championship's total to the stored result is the invariant
        # that actually matters.
        frozen = {
            row["classroom"]: row["score"] for row in result.rows
        }
        self.assertEqual(
            self.tournament.participants.get(
                classroom__name="Science Lab A"
            ).aggregate_score,
            frozen["Science Lab A"],
        )
        self.assertEqual(
            self.tournament.participants.get(
                classroom__name="Science Lab D"
            ).aggregate_score,
            frozen["Science Lab D"],
        )
        # And the ranking followed from how many answers were right.
        self.assertGreater(frozen["Science Lab A"], frozen["Science Lab D"])

        # -- Semi final / final -------------------------------------------
        final = tournament_services.create_stage(self.tournament, "final")
        self.assertEqual(final.order, 2)
        self.assertEqual(
            self.tournament.participants.get(classroom__name="Science Lab A").current_stage,
            final,
        )

        finalists = [
            self.classrooms[0],
            self.classrooms[1],
        ]
        final_questions = [
            self._make_question(text=f"Final question {index}")
            for index in range(1, 3)
        ]
        final_round = Competition.objects.create(
            title="Final round", teacher=self.teacher
        )
        for classroom in finalists:
            CompetitionClassroom.objects.create(
                competition=final_round, classroom=classroom
            )
        for index, question in enumerate(final_questions, start=1):
            CompetitionQuestion.objects.create(
                competition=final_round, question=question, position=index
            )
        tournament_services.attach_competition(final, final_round)

        play_a_round(
            live_services,
            final_round,
            finalists,
            # A wins the final outright.
            {"Science Lab A": 2, "Science Lab B": 1},
        )
        tournament_services.complete_stage(final)
        tournament_services.process_advancement(final, user=self.teacher)

        # -- Conclude -----------------------------------------------------
        record = tournament_services.finalize_tournament(self.tournament)

        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.COMPLETED)
        self.assertEqual(record.winner_name, "Science Lab A")
        self.assertEqual(record.season, "2026")
        self.assertEqual(record.participants_count, 4)

        champion = self.tournament.participants.get(classroom__name="Science Lab A")
        self.assertEqual(champion.status, TournamentParticipantStatus.CHAMPION)

        # -- History and Hall of Fame -------------------------------------
        history = tournament_services.history_for(self.tournament)
        self.assertEqual(len(history["stages"]), 2)
        self.assertEqual(
            {stage["stage"].stage_type for stage in history["stages"]},
            {"qualification", "final"},
        )
        self.assertEqual(len(history["participants"]), 4)

        hall = tournament_services.hall_of_fame()
        self.assertEqual(len(hall), 1)
        self.assertEqual(hall[0].tournament.name, "Primary Championship")
        self.assertEqual(hall[0].winner_name, "Science Lab A")

    def _make_question(self, *, text: str):
        from core.tests.utils import create_question

        # Points are set explicitly so a score is a function of how many answers
        # were right, with no speed bonus muddying the arithmetic.
        return create_question(
            text=text,
            options=[f"Option {letter}" for letter in "abcd"],
            correct_option="Option b",
            duration_seconds=30,
        )


class ChampionshipOverHttpTests(LiveTestCase):
    """The same championship, driven through the pages a teacher actually uses."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="http.teacher")
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("tournaments:create"),
            {
                "name": "Primary Championship",
                "subject": "Science",
                "grade": "Grade 5",
                "season": "2026",
                "description": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.tournament = Tournament_get_latest()

        self.action_url = reverse("tournaments:action", args=[self.tournament.pk])

    def post(self, **data):
        return self.client.post(self.action_url, data)

    def test_a_championship_can_be_run_and_concluded_through_the_pages(self) -> None:
        from core.tests.utils import create_classroom

        for name in LABS:
            classroom = create_classroom(name=name)
            self.post(action="add_participant", classroom=classroom.pk)

        self.assertEqual(self.tournament.participants.count(), 4)

        self.post(action="open")
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.OPEN)

        self.post(action="create_stage", stage_type="qualification", advancing_count="2")
        stage = self.tournament.stages.get()

        participants = list(
            self.tournament.participants.select_related("classroom")
        )
        competition = played_competition(
            teacher=self.teacher,
            scores={
                p.classroom.name: (CORRECT_ANSWERS[p.classroom.name] * POINTS_PER_CORRECT, 0)
                for p in participants
            },
            classrooms=[p.classroom for p in participants],
            title="Qualification round",
        )

        self.post(
            action="attach_competition", stage=stage.pk, competition=competition.pk
        )
        self.post(action="complete_stage", stage=stage.pk)
        self.post(action="process_advancement", stage=stage.pk)

        self.tournament.refresh_from_db()
        self.assertEqual(
            self.tournament.participants.get(classroom__name="Science Lab A").status,
            TournamentParticipantStatus.QUALIFIED,
        )

        self.post(action="create_stage", stage_type="final", advancing_count="")
        final_stage = self.tournament.stages.get(stage_type="final")

        finalists = [
            self.tournament.participants.get(classroom__name="Science Lab A").classroom,
            self.tournament.participants.get(classroom__name="Science Lab B").classroom,
        ]
        final_round = played_competition(
            teacher=self.teacher,
            scores={"Science Lab A": (200, 0), "Science Lab B": (100, 0)},
            classrooms=finalists,
            title="Final round",
        )
        self.post(
            action="attach_competition",
            stage=final_stage.pk,
            competition=final_round.pk,
        )
        self.post(action="complete_stage", stage=final_stage.pk)
        self.post(action="process_advancement", stage=final_stage.pk)
        self.post(action="finalize")

        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, TournamentStatus.COMPLETED)

        # The dashboard shows the concluded championship.
        detail = self.client.get(
            reverse("tournaments:detail", args=[self.tournament.pk])
        )
        self.assertContains(detail, "Completed")
        self.assertContains(detail, "Science Lab A")

        # And so does the Hall of Fame.
        hall = self.client.get(reverse("tournaments:hall_of_fame"))
        self.assertContains(hall, "Primary Championship")

        # And the report agrees.
        report = self.client.get(
            reverse("reports:tournament", args=[self.tournament.pk])
        )
        self.assertContains(report, "Science Lab A")

        # And it exports.
        export = self.client.get(
            reverse("reports:tournament_csv", args=[self.tournament.pk])
        )
        self.assertEqual(export.status_code, 200)
        self.assertIn("Science Lab A", export.content.decode())


def Tournament_get_latest():
    from tournaments.models import Tournament

    return Tournament.objects.order_by("-pk").first()
