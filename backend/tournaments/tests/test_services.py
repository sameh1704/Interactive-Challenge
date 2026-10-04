"""Tournament services: stages, participants, advancement, ties and completion.

Every rule that decides a championship outcome is tested here, without a browser
and without a WebSocket. The tests build finished rounds through
:func:`core.tests.factories.played_competition`, which freezes a real
:class:`scoring.models.CompetitionResult` - so what is being advanced from is the
same stored result the live engine produces, not a convenient stub.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase

from competitions.models import Competition, CompetitionState
from tournaments import services
from tournaments.models import (
    AdvancementStatus,
    StageAdvancement,
    Tournament,
    TournamentParticipant,
    TournamentParticipantStatus,
    TournamentRecord,
    TournamentStage,
    TournamentStageStatus,
    TournamentStageType,
    TournamentStatus,
)

from core.tests.utils import (
    add_participants,
    attach_competition,
    create_administrator,
    create_classroom,
    create_competition,
    create_stage,
    create_teacher,
    create_tournament,
    played_competition,
)


def full_championship(scores, advancing=2):
    """A tournament with four classrooms and one qualification stage.

    ``scores`` maps classroom name to ``(points, speed_bonus)``. The classrooms
    are created once, as tournament participants, and the same objects then play
    the round - which is the whole point of the design: a participant is a
    reference to a real classroom, not a copy of one.
    """
    tournament = create_tournament()
    participants = add_participants(tournament, *scores)
    stage = create_stage(tournament, "qualification", advancing_count=advancing)

    competition = played_competition(
        scores=scores, classrooms=[p.classroom for p in participants]
    )
    attach_competition(stage, competition)
    return tournament, stage, competition


class TournamentCreationTests(TestCase):
    """1. Tournament creation."""

    def test_a_tournament_starts_as_a_draft(self) -> None:
        teacher = create_teacher()

        tournament = create_tournament(created_by=teacher, name="Science Cup")

        self.assertEqual(tournament.name, "Science Cup")
        self.assertEqual(tournament.status, TournamentStatus.DRAFT)
        self.assertEqual(tournament.created_by, teacher)

    def test_a_tournament_requires_a_name(self) -> None:
        with self.assertRaises(services.TournamentError) as caught:
            services.create_tournament(name="   ", created_by=create_teacher())

        self.assertEqual(caught.exception.code, "name_required")

    def test_the_end_date_cannot_precede_the_start(self) -> None:
        from datetime import date

        with self.assertRaises(services.TournamentError) as caught:
            services.create_tournament(
                name="Backwards",
                created_by=create_teacher(),
                start_date=date(2026, 5, 1),
                end_date=date(2026, 4, 1),
            )

        self.assertEqual(caught.exception.code, "invalid_dates")

    def test_a_draft_cannot_be_opened_without_participants(self) -> None:
        tournament = create_tournament()

        with self.assertRaises(services.TournamentError) as caught:
            services.open_tournament(tournament)

        self.assertEqual(caught.exception.code, "no_participants")

    def test_opening_a_tournament_with_participants_succeeds(self) -> None:
        tournament = create_tournament()
        add_participants(tournament, "Science Lab A")

        services.open_tournament(tournament)

        self.assertEqual(tournament.status, TournamentStatus.OPEN)

    def test_a_completed_tournament_cannot_be_cancelled(self) -> None:
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0),
             "Science Lab C": (200, 0), "Science Lab D": (150, 0)}
        )
        services.complete_stage(stage)
        services.process_advancement(stage)
        services.finalize_tournament(tournament)

        with self.assertRaises(services.TournamentError) as caught:
            services.cancel_tournament(tournament)

        self.assertEqual(caught.exception.code, "already_completed")


class TournamentPermissionTests(TestCase):
    """2. Tournament permissions."""

    def setUp(self) -> None:
        self.owner = create_teacher(username="owner")
        self.other = create_teacher(username="other")
        self.administrator = create_administrator()
        self.tournament = create_tournament(created_by=self.owner)

    def test_the_owning_teacher_may_manage(self) -> None:
        services.assert_can_manage(self.tournament, self.owner)

    def test_an_administrator_may_manage_any_tournament(self) -> None:
        services.assert_can_manage(self.tournament, self.administrator)

    def test_another_teacher_may_not(self) -> None:
        with self.assertRaises(services.TournamentError) as caught:
            services.assert_can_manage(self.tournament, self.other)

        self.assertEqual(caught.exception.code, "not_authorised")

    def test_an_anonymous_visitor_may_not(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        with self.assertRaises(services.TournamentError) as caught:
            services.assert_can_manage(self.tournament, AnonymousUser())

        self.assertEqual(caught.exception.code, "not_signed_in")

    def test_a_deactivated_account_may_not(self) -> None:
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])

        with self.assertRaises(services.TournamentError) as caught:
            services.assert_can_manage(self.tournament, self.owner)

        self.assertEqual(caught.exception.code, "not_signed_in")

    def test_the_queryset_hides_other_teachers_tournaments(self) -> None:
        self.assertIn(
            self.tournament, Tournament.objects.for_user(self.owner)
        )
        self.assertNotIn(
            self.tournament, Tournament.objects.for_user(self.other)
        )
        self.assertIn(
            self.tournament, Tournament.objects.for_user(self.administrator)
        )


class StageCreationTests(TestCase):
    """3-4. Stage creation and ordering."""

    def setUp(self) -> None:
        self.tournament = create_tournament()

    def test_a_stage_is_created_pending_at_the_end(self) -> None:
        stage = services.create_stage(self.tournament, "qualification")

        self.assertEqual(stage.stage_type, TournamentStageType.QUALIFICATION)
        self.assertEqual(stage.order, 1)
        self.assertEqual(stage.status, TournamentStageStatus.PENDING)

    def test_stages_are_numbered_in_creation_order(self) -> None:
        qualification = services.create_stage(self.tournament, "qualification")
        semi = services.create_stage(self.tournament, "semi_final")
        final = services.create_stage(self.tournament, "final")

        self.assertEqual([s.order for s in (qualification, semi, final)], [1, 2, 3])
        self.assertEqual(
            [s.pk for s in self.tournament.stages.order_by("order")],
            [qualification.pk, semi.pk, final.pk],
        )

    def test_an_explicit_order_is_honoured(self) -> None:
        services.create_stage(self.tournament, "final", order=3)
        qualification = services.create_stage(self.tournament, "qualification", order=1)

        self.assertEqual(qualification.order, 1)

    def test_two_stages_cannot_share_a_position(self) -> None:
        services.create_stage(self.tournament, "qualification", order=1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            services.create_stage(self.tournament, "semi_final", order=1)

    def test_a_stage_type_can_appear_only_once(self) -> None:
        services.create_stage(self.tournament, "qualification")

        with self.assertRaises(IntegrityError), transaction.atomic():
            services.create_stage(self.tournament, "qualification")

    def test_an_unknown_stage_type_is_refused(self) -> None:
        with self.assertRaises(services.TournamentError) as caught:
            services.create_stage(self.tournament, "play_off")

        self.assertEqual(caught.exception.code, "unknown_stage_type")

    def test_next_stage_follows_the_order(self) -> None:
        qualification = services.create_stage(self.tournament, "qualification")
        final = services.create_stage(self.tournament, "final")

        self.assertEqual(qualification.next_stage, final)
        self.assertIsNone(final.next_stage)

    def test_the_current_stage_is_the_first_incomplete_one(self) -> None:
        qualification = services.create_stage(self.tournament, "qualification")
        final = services.create_stage(self.tournament, "final")

        self.assertEqual(self.tournament.current_stage, qualification)

        qualification.status = TournamentStageStatus.COMPLETED
        qualification.save(update_fields=["status"])

        self.assertEqual(self.tournament.current_stage, final)


class ParticipantTests(TestCase):
    """5-6. Participants and duplicate prevention."""

    def setUp(self) -> None:
        self.tournament = create_tournament()

    def test_a_classroom_can_be_added(self) -> None:
        classroom = create_classroom(name="Science Lab A")

        participant = services.add_participant(self.tournament, classroom)

        self.assertEqual(participant.classroom, classroom)
        self.assertEqual(participant.status, TournamentParticipantStatus.ENTERED)

    def test_the_same_classroom_cannot_enter_twice(self) -> None:
        classroom = create_classroom(name="Science Lab A")
        services.add_participant(self.tournament, classroom)

        with self.assertRaises(services.TournamentError) as caught:
            services.add_participant(self.tournament, classroom)

        self.assertEqual(caught.exception.code, "duplicate_participant")
        self.assertEqual(self.tournament.participants.count(), 1)

    def test_the_database_refuses_a_duplicate_entry_too(self) -> None:
        """A service check alone would still let two simultaneous adds through."""
        classroom = create_classroom(name="Science Lab A")
        services.add_participant(self.tournament, classroom)

        with self.assertRaises(IntegrityError), transaction.atomic():
            TournamentParticipant.objects.create(
                tournament=self.tournament, classroom=classroom
            )

    def test_a_classroom_may_join_two_different_tournaments(self) -> None:
        classroom = create_classroom(name="Science Lab A")
        other = create_tournament(name="Science Cup")

        services.add_participant(self.tournament, classroom)
        services.add_participant(other, classroom)

        self.assertEqual(classroom.tournament_entries.count(), 2)

    def test_a_completed_tournament_takes_no_new_participants(self) -> None:
        tournament = create_tournament()
        add_participants(tournament, "Science Lab A")
        services.open_tournament(tournament)
        tournament.status = TournamentStatus.COMPLETED
        tournament.save(update_fields=["status"])

        with self.assertRaises(services.TournamentError) as caught:
            services.add_participant(tournament, create_classroom(name="Science Lab Z"))

        self.assertEqual(caught.exception.code, "tournament_locked")

    def test_a_running_tournament_takes_no_new_participants(self) -> None:
        tournament = create_tournament()
        add_participants(tournament, "Science Lab A")
        tournament.status = TournamentStatus.RUNNING
        tournament.save(update_fields=["status"])

        with self.assertRaises(services.TournamentError) as caught:
            services.add_participant(tournament, create_classroom(name="Science Lab Z"))

        self.assertEqual(caught.exception.code, "tournament_locked")

    def test_a_classroom_can_be_withdrawn_but_keeps_its_score(self) -> None:
        tournament, _, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0)}
        )
        participant = tournament.participants.get(classroom__name="Science Lab B")
        participant.aggregate_score = 250
        participant.save(update_fields=["aggregate_score"])

        services.withdraw_participant(participant)

        participant.refresh_from_db()
        self.assertEqual(participant.status, TournamentParticipantStatus.WITHDRAWN)
        self.assertEqual(participant.aggregate_score, 250)


class CompetitionAttachmentTests(TestCase):
    """7. Assigning an existing competition to a stage."""

    def setUp(self) -> None:
        self.tournament = create_tournament()
        self.participants = add_participants(
            self.tournament, "Science Lab A", "Science Lab B"
        )
        self.stage = services.create_stage(self.tournament, "qualification")

    def test_an_existing_competition_can_be_attached(self) -> None:
        classrooms = [p.classroom for p in self.participants]
        competition = create_competition(classrooms=classrooms)

        link = services.attach_competition(self.stage, competition)

        self.assertEqual(link.stage, self.stage)
        self.assertEqual(link.competition, competition)
        self.assertEqual(list(self.stage.attached_competitions), [competition])

    def test_the_stage_status_reflects_the_attached_round(self) -> None:
        classrooms = [p.classroom for p in self.participants]
        competition = create_competition(classrooms=classrooms)

        services.attach_competition(self.stage, competition)

        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, TournamentStageStatus.RUNNING)

    def test_a_competition_cannot_belong_to_two_stages(self) -> None:
        classrooms = [p.classroom for p in self.participants]
        competition = create_competition(classrooms=classrooms)
        services.attach_competition(self.stage, competition)
        second = services.create_stage(self.tournament, "semi_final")

        with self.assertRaises(services.TournamentError) as caught:
            services.attach_competition(second, competition)

        self.assertEqual(caught.exception.code, "competition_in_use")

    def test_a_classroom_outside_the_tournament_is_refused(self) -> None:
        outsider = create_classroom(name="Science Lab Z")
        competition = create_competition(
            classrooms=[self.participants[0].classroom, outsider]
        )

        with self.assertRaises(services.TournamentError) as caught:
            services.attach_competition(self.stage, competition)

        self.assertEqual(caught.exception.code, "classroom_not_a_participant")

    def test_a_completed_stage_refuses_new_competitions(self) -> None:
        self.stage.status = TournamentStageStatus.COMPLETED
        self.stage.save(update_fields=["status"])
        classrooms = [p.classroom for p in self.participants]
        competition = create_competition(classrooms=classrooms)

        with self.assertRaises(services.TournamentError) as caught:
            services.attach_competition(self.stage, competition)

        self.assertEqual(caught.exception.code, "stage_locked")


class StageCompletionTests(TestCase):
    """8. Stage completion validation."""

    def setUp(self) -> None:
        self.tournament = create_tournament()
        self.participants = add_participants(
            self.tournament, "Science Lab A", "Science Lab B"
        )
        self.stage = services.create_stage(
            self.tournament, "qualification", advancing_count=1
        )
        self.classrooms = [p.classroom for p in self.participants]

    def test_a_stage_with_no_competition_cannot_complete(self) -> None:
        with self.assertRaises(services.TournamentError) as caught:
            services.complete_stage(self.stage)

        self.assertEqual(caught.exception.code, "no_competitions")

    def test_a_stage_with_a_running_round_cannot_complete(self) -> None:
        running = create_competition(classrooms=self.classrooms)
        services.attach_competition(self.stage, running)

        with self.assertRaises(services.TournamentError) as caught:
            services.complete_stage(self.stage)

        self.assertEqual(caught.exception.code, "competition_running")

    def test_a_round_without_a_frozen_result_does_not_count_as_finished(self) -> None:
        """State alone is not enough: advancement needs the stored standings."""
        competition = create_competition(classrooms=self.classrooms)
        services.attach_competition(self.stage, competition)
        competition.state = CompetitionState.FINISHED
        competition.save(update_fields=["state"])

        with self.assertRaises(services.TournamentError) as caught:
            services.complete_stage(self.stage)

        self.assertEqual(caught.exception.code, "competition_running")

    def test_a_stage_completes_once_its_round_is_finished(self) -> None:
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=self.classrooms,
        )
        services.attach_competition(self.stage, competition)

        services.complete_stage(self.stage)

        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, TournamentStageStatus.COMPLETED)
        self.assertEqual(self.tournament.status, TournamentStatus.RUNNING)

    def test_advancement_is_refused_before_the_stage_completes(self) -> None:
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=self.classrooms,
        )
        services.attach_competition(self.stage, competition)

        with self.assertRaises(services.TournamentError) as caught:
            services.calculate_advancement(self.stage)

        self.assertEqual(caught.exception.code, "stage_not_completed")


class AdvancementTests(TestCase):
    """9-12, 14. Calculation, configurable count, duplicates and the final stage."""

    def test_the_top_n_advance(self) -> None:
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0),
             "Science Lab C": (200, 0), "Science Lab D": (150, 0)},
            advancing=2,
        )
        services.complete_stage(stage)

        plan = services.calculate_advancement(stage)

        self.assertEqual(
            [s.classroom_name for s in plan.advancing],
            ["Science Lab A", "Science Lab B"],
        )
        self.assertEqual(
            [s.classroom_name for s in plan.eliminated],
            ["Science Lab C", "Science Lab D"],
        )

    def test_the_advancing_count_is_configurable_not_hard_coded(self) -> None:
        for advancing in (1, 2, 3):
            with self.subTest(advancing=advancing):
                # Distinct classroom names per round of the loop: a championship
                # may not contain the same room twice, and neither may one test.
                suffix = str(advancing)
                tournament, stage, _ = full_championship(
                    {
                        f"Science Lab A{suffix}": (300, 0),
                        f"Science Lab B{suffix}": (250, 0),
                        f"Science Lab C{suffix}": (200, 0),
                        f"Science Lab D{suffix}": (150, 0),
                    },
                    advancing=advancing,
                )
                services.complete_stage(stage)

                plan = services.calculate_advancement(stage)

                self.assertEqual(len(plan.advancing), advancing)

    def test_standings_rank_by_score_then_correct_answers(self) -> None:
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0),
             "Science Lab C": (200, 0), "Science Lab D": (150, 0)},
            advancing=2,
        )
        services.complete_stage(stage)

        plan = services.calculate_advancement(stage)

        self.assertEqual(
            [(s.rank, s.classroom_name) for s in plan.standings],
            [(1, "Science Lab A"), (2, "Science Lab B"),
             (3, "Science Lab C"), (4, "Science Lab D")],
        )

    def test_processing_marks_the_winners_and_eliminates_the_rest(self) -> None:
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0),
             "Science Lab C": (200, 0), "Science Lab D": (150, 0)},
            advancing=2,
        )
        services.complete_stage(stage)

        services.process_advancement(stage)

        statuses = {
            p.classroom.name: p.status
            for p in tournament.participants.select_related("classroom")
        }
        self.assertEqual(statuses["Science Lab A"], TournamentParticipantStatus.QUALIFIED)
        self.assertEqual(statuses["Science Lab B"], TournamentParticipantStatus.QUALIFIED)
        self.assertEqual(statuses["Science Lab C"], TournamentParticipantStatus.ELIMINATED)
        self.assertEqual(statuses["Science Lab D"], TournamentParticipantStatus.ELIMINATED)

        scores = {
            p.classroom.name: p.aggregate_score
            for p in tournament.participants.select_related("classroom")
        }
        self.assertEqual(scores["Science Lab A"], 300)
        self.assertEqual(scores["Science Lab D"], 150)

    def test_the_eliminated_classroom_records_where_it_went_out(self) -> None:
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            advancing=1,
        )
        services.complete_stage(stage)

        services.process_advancement(stage)

        eliminated = tournament.participants.get(classroom__name="Science Lab B")
        self.assertEqual(eliminated.eliminated_at_stage, stage)

    def test_an_advancing_classroom_moves_to_the_next_stage(self) -> None:
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            advancing=1,
        )
        final = services.create_stage(tournament, "final")
        services.complete_stage(stage)

        services.process_advancement(stage)

        winner = tournament.participants.get(classroom__name="Science Lab A")
        self.assertEqual(winner.current_stage, final)

    def test_advancement_cannot_be_processed_twice(self) -> None:
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            advancing=1,
        )
        services.complete_stage(stage)
        services.process_advancement(stage)

        with self.assertRaises(services.TournamentError) as caught:
            services.calculate_advancement(stage)

        self.assertEqual(caught.exception.code, "already_processed")
        self.assertEqual(StageAdvancement.objects.filter(stage=stage).count(), 1)

    def test_too_few_participants_for_the_configured_cut(self) -> None:
        tournament = create_tournament()
        add_participants(tournament, "Science Lab A", "Science Lab B")
        stage = services.create_stage(
            tournament, "qualification", advancing_count=4
        )
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=[p.classroom for p in tournament.participants.select_related("classroom")],
        )
        services.attach_competition(stage, competition)
        services.complete_stage(stage)

        with self.assertRaises(services.TournamentError) as caught:
            services.calculate_advancement(stage)

        self.assertEqual(caught.exception.code, "insufficient_participants")

    def test_a_final_advances_everyone_who_reached_it(self) -> None:
        tournament = create_tournament()
        add_participants(tournament, "Science Lab A", "Science Lab B")
        final = services.create_stage(tournament, "final", advancing_count=None)
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=[p.classroom for p in tournament.participants.select_related("classroom")],
        )
        services.attach_competition(final, competition)
        services.complete_stage(final)

        plan = services.calculate_advancement(final)

        self.assertEqual(len(plan.advancing), 2)
        self.assertEqual(plan.eliminated, [])
        self.assertFalse(plan.has_tie)

    def test_a_round_nobody_scored_ranks_everyone_on_zero(self) -> None:
        """A room that took part and scored nothing is still ranked.

        ``scoring.leaderboard`` deliberately shows a class sitting at zero,
        because "nobody answered" is information a room needs. A championship cut
        inherits that: silently dropping an unranked room would make a
        championship look complete when a screen had in fact failed.
        """
        tournament = create_tournament()
        participants = add_participants(tournament, "Science Lab A", "Science Lab B")
        stage = services.create_stage(tournament, "final", advancing_count=None)
        competition = create_competition(
            classrooms=[p.classroom for p in participants]
        )
        services.attach_competition(stage, competition)
        from scoring import services as scoring_services

        competition.state = CompetitionState.FINISHED
        competition.save(update_fields=["state"])
        scoring_services.finalise(competition)
        services.complete_stage(stage)

        plan = services.calculate_advancement(stage)

        self.assertEqual(len(plan.standings), 2)
        self.assertEqual([s.score for s in plan.standings], [0, 0])

    def test_a_championship_nobody_scored_has_no_champion(self) -> None:
        """Nobody answered, so nobody wins.

        Naming a champion from an unearned round would announce a result to a
        school that was not earned - the same rule the live leaderboard applies.
        """
        tournament = create_tournament()
        participants = add_participants(tournament, "Science Lab A", "Science Lab B")
        stage = services.create_stage(tournament, "final", advancing_count=None)
        competition = create_competition(
            classrooms=[p.classroom for p in participants]
        )
        services.attach_competition(stage, competition)
        from scoring import services as scoring_services

        competition.state = CompetitionState.FINISHED
        competition.save(update_fields=["state"])
        scoring_services.finalise(competition)
        services.complete_stage(stage)
        services.process_advancement(stage)

        record = services.finalize_tournament(tournament)

        self.assertEqual(record.winner_name, "")
        self.assertIsNone(record.winner)


class TieHandlingTests(TestCase):
    """13. Tie handling: explicit, deterministic, never a silent selection."""

    def tied_championship(self):
        """Four classrooms where the cut falls between two on identical figures."""
        return full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0),
             "Science Lab C": (250, 0), "Science Lab D": (150, 0)},
            advancing=2,
        )

    def test_a_tie_is_reported_rather_than_broken(self) -> None:
        _, stage, _ = self.tied_championship()
        services.complete_stage(stage)

        plan = services.calculate_advancement(stage)

        self.assertTrue(plan.has_tie)
        self.assertEqual(plan.advancing, [])
        self.assertEqual(
            sorted(s.classroom_name for s in plan.tie),
            ["Science Lab B", "Science Lab C"],
        )

    def test_a_tie_is_recorded_when_processing(self) -> None:
        _, stage, _ = self.tied_championship()
        services.complete_stage(stage)

        advancement = services.process_advancement(stage)

        self.assertEqual(advancement.status, AdvancementStatus.TIE)
        self.assertEqual(
            sorted(advancement.tied_classroom_ids),
            sorted(
                stage.tournament.participants.filter(
                    classroom__name__in=["Science Lab B", "Science Lab C"]
                ).values_list("classroom_id", flat=True)
            ),
        )
        self.assertEqual(advancement.tie_detail["score"], 250)

    def test_nobody_advances_while_a_tie_is_unresolved(self) -> None:
        tournament, stage, _ = self.tied_championship()
        services.complete_stage(stage)

        services.process_advancement(stage)

        statuses = {
            p.classroom.name: p.status
            for p in tournament.participants.select_related("classroom")
        }
        self.assertEqual(set(statuses.values()), {TournamentParticipantStatus.ENTERED})

    def test_tied_classrooms_share_a_rank(self) -> None:
        _, stage, _ = self.tied_championship()
        services.complete_stage(stage)

        plan = services.calculate_advancement(stage)

        ranks = {s.classroom_name: s.rank for s in plan.standings}
        self.assertEqual(ranks["Science Lab B"], ranks["Science Lab C"])
        self.assertEqual(ranks["Science Lab A"], 1)

    def test_an_administrator_can_resolve_a_tie(self) -> None:
        tournament, stage, _ = self.tied_championship()
        services.complete_stage(stage)
        services.process_advancement(stage)
        administrator = create_administrator()

        chosen = [
            tournament.participants.get(classroom__name=name).classroom_id
            for name in ("Science Lab A", "Science Lab C")
        ]
        services.resolve_tie(stage, chosen, user=administrator)

        statuses = {
            p.classroom.name: p.status
            for p in tournament.participants.select_related("classroom")
        }
        self.assertEqual(statuses["Science Lab A"], TournamentParticipantStatus.QUALIFIED)
        self.assertEqual(statuses["Science Lab C"], TournamentParticipantStatus.QUALIFIED)
        self.assertEqual(statuses["Science Lab B"], TournamentParticipantStatus.ELIMINATED)

        stage.advancement.refresh_from_db()
        self.assertEqual(stage.advancement.status, AdvancementStatus.PROCESSED)
        self.assertEqual(stage.advancement.resolved_by, administrator)
        self.assertIsNotNone(stage.advancement.resolved_at)

    def test_a_teacher_cannot_resolve_a_tie(self) -> None:
        tournament, stage, _ = self.tied_championship()
        services.complete_stage(stage)
        services.process_advancement(stage)
        chosen = tournament.participants.get(classroom__name="Science Lab C")

        with self.assertRaises(services.TournamentError) as caught:
            services.resolve_tie(
                stage, [chosen.classroom_id], user=create_teacher(username="t2")
            )

        self.assertEqual(caught.exception.code, "not_authorised")

    def test_resolution_may_not_promote_a_classroom_that_was_not_tied(self) -> None:
        """Resolving a tie is not a way to pick a different result.

        Lab D finished below the cut-off and was never tied for it, so advancing
        it in place of a tied classroom would be deciding the result rather than
        resolving an ambiguity.
        """
        tournament, stage, _ = self.tied_championship()
        services.complete_stage(stage)
        services.process_advancement(stage)
        chosen = [
            tournament.participants.get(classroom__name=name).classroom_id
            for name in ("Science Lab A", "Science Lab D")
        ]

        with self.assertRaises(services.TournamentError) as caught:
            services.resolve_tie(stage, chosen, user=create_administrator())

        self.assertEqual(caught.exception.code, "not_a_tie_classroom")

    def test_resolution_may_not_drop_a_higher_scoring_classroom(self) -> None:
        """The tied places are the only ones open; the rest are already through."""
        tournament, stage, _ = self.tied_championship()
        services.complete_stage(stage)
        services.process_advancement(stage)
        chosen = [
            tournament.participants.get(classroom__name=name).classroom_id
            for name in ("Science Lab B", "Science Lab C")
        ]

        with self.assertRaises(services.TournamentError) as caught:
            services.resolve_tie(stage, chosen, user=create_administrator())

        self.assertEqual(caught.exception.code, "higher_classroom_excluded")

    def test_resolution_must_name_exactly_the_configured_number(self) -> None:
        tournament, stage, _ = self.tied_championship()
        services.complete_stage(stage)
        services.process_advancement(stage)
        tied = tournament.participants.get(classroom__name="Science Lab C")

        with self.assertRaises(services.TournamentError) as caught:
            services.resolve_tie(stage, [tied.classroom_id], user=create_administrator())

        self.assertEqual(caught.exception.code, "wrong_number")

    def test_resolution_refuses_a_stage_with_no_tie(self) -> None:
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0),
             "Science Lab C": (200, 0), "Science Lab D": (150, 0)},
            advancing=2,
        )
        services.complete_stage(stage)
        chosen = [
            tournament.participants.get(classroom__name=name).classroom_id
            for name in ("Science Lab A", "Science Lab B")
        ]

        with self.assertRaises(services.TournamentError) as caught:
            services.resolve_tie(stage, chosen, user=create_administrator())

        self.assertEqual(caught.exception.code, "no_tie")

    def test_a_tie_is_broken_by_correct_answers_when_scores_match(self) -> None:
        """Equal points but a different number right is not a tie.

        The documented rule is score, then correct answers, so this is decided
        automatically rather than escalated - and it is decided the same way every
        time. The scores are made equal by a speed bonus, which is the realistic
        way two classrooms end up level on points but not on knowledge.
        """
        from django.utils import timezone

        from core.tests.utils import create_answer, create_question
        from scoring import services as scoring_services

        tournament = create_tournament()
        participants = add_participants(tournament, "Science Lab A", "Science Lab B")
        stage = services.create_stage(tournament, "qualification", advancing_count=1)
        lab_a, lab_b = [p.classroom for p in participants]

        questions = [create_question(text="Q1"), create_question(text="Q2")]
        competition = create_competition(
            classrooms=[lab_a, lab_b], questions=questions
        )

        now = timezone.now()
        # Lab A: both right, no bonus -> 200.
        for position, question in zip((1, 2), questions):
            create_answer(
                competition, lab_a, question=question, position=position,
                points=100, speed_bonus=0, is_correct=True, submitted_at=now,
            )
        # Lab B: one right but fast enough to level the total -> 200 as well.
        create_answer(
            competition, lab_b, question=questions[0], position=1,
            points=100, speed_bonus=100, is_correct=True, submitted_at=now,
        )
        create_answer(
            competition, lab_b, question=questions[1], position=2,
            points=0, speed_bonus=0, is_correct=False, submitted_at=now,
        )

        competition.state = CompetitionState.FINISHED
        competition.save(update_fields=["state"])
        scoring_services.finalise(competition)

        services.attach_competition(stage, competition)
        services.complete_stage(stage)

        plan = services.calculate_advancement(stage)

        self.assertFalse(plan.has_tie)
        self.assertEqual([s.score for s in plan.standings], [200, 200])
        self.assertEqual(
            [s.classroom_name for s in plan.advancing], ["Science Lab A"]
        )


class TournamentCompletionTests(TestCase):
    """15. Tournament completion and its historical record."""

    def played_championship(self):
        tournament, stage, _ = full_championship(
            {"Science Lab A": (300, 0), "Science Lab B": (250, 0),
             "Science Lab C": (200, 0), "Science Lab D": (150, 0)},
            advancing=2,
        )
        services.complete_stage(stage)
        services.process_advancement(stage)
        return tournament, stage

    def test_a_tournament_with_no_completed_stage_cannot_be_finalized(self) -> None:
        tournament = create_tournament()
        add_participants(tournament, "Science Lab A")

        with self.assertRaises(services.TournamentError) as caught:
            services.finalize_tournament(tournament)

        self.assertEqual(caught.exception.code, "no_completed_stage")

    def test_finalizing_records_the_winner_and_completes_the_tournament(self) -> None:
        tournament, _ = self.played_championship()

        record = services.finalize_tournament(tournament)

        tournament.refresh_from_db()
        self.assertEqual(tournament.status, TournamentStatus.COMPLETED)
        self.assertEqual(record.winner_name, "Science Lab A")
        self.assertEqual(record.winner.display_name, "Science Lab A")
        self.assertEqual(record.season, tournament.season)
        self.assertEqual(record.participants_count, 4)

    def test_the_winning_classroom_is_marked_champion(self) -> None:
        tournament, _ = self.played_championship()

        services.finalize_tournament(tournament)

        champion = tournament.participants.get(classroom__name="Science Lab A")
        self.assertEqual(champion.status, TournamentParticipantStatus.CHAMPION)

    def test_the_record_freezes_the_final_standings(self) -> None:
        tournament, _ = self.played_championship()

        record = services.finalize_tournament(tournament)

        self.assertEqual(
            [(row["rank"], row["classroom"]) for row in record.rows],
            [(1, "Science Lab A"), (2, "Science Lab B")],
        )

    def test_the_frozen_table_agrees_with_the_standings_it_was_frozen_from(self) -> None:
        """The record must not contradict itself about who won.

        The standings are computed before the winner is known and then recomputed
        once it is. Freezing the first pass would store a champion as "qualified"
        in the very record that names it the winner.
        """
        tournament, _ = self.played_championship()

        record = services.finalize_tournament(tournament)

        frozen = {row["participant_id"]: row for row in record.rows}
        finalists = tournament.participants.filter(
            status__in=[
                TournamentParticipantStatus.QUALIFIED,
                TournamentParticipantStatus.CHAMPION,
            ]
        )
        for participant in finalists:
            with self.subTest(classroom=participant.classroom.name):
                self.assertEqual(frozen[participant.pk]["status"], participant.status)

        champion = tournament.participants.get(
            status=TournamentParticipantStatus.CHAMPION
        )
        self.assertEqual(frozen[champion.pk]["status"], "champion")

        # A classroom knocked out along the way is not a finalist, so it has no row
        # at all rather than a row carrying a stale status.
        for participant in tournament.participants.filter(
            status=TournamentParticipantStatus.ELIMINATED
        ):
            with self.subTest(classroom=participant.classroom.name):
                self.assertNotIn(participant.pk, frozen)

    def test_finalizing_twice_refuses_rather_than_duplicating(self) -> None:
        tournament, _ = self.played_championship()
        services.finalize_tournament(tournament)

        with self.assertRaises(services.TournamentError) as caught:
            services.finalize_tournament(tournament)

        self.assertEqual(caught.exception.code, "already_completed")
        self.assertEqual(TournamentRecord.objects.count(), 1)

    def test_a_cancelled_tournament_has_no_final_result(self) -> None:
        tournament, stage = self.played_championship()
        services.cancel_tournament(tournament)

        with self.assertRaises(services.TournamentError) as caught:
            services.finalize_tournament(tournament)

        self.assertEqual(caught.exception.code, "cancelled")


class HistoryTests(TestCase):
    """16-17. Historical records and the Hall of Fame."""

    def test_history_describes_the_tournament_and_its_stages(self) -> None:
        tournament = create_tournament(name="Primary Championship", season="2025/2026")
        add_participants(tournament, "Science Lab A", "Science Lab B")
        stage = services.create_stage(tournament, "qualification", advancing_count=1)
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=[p.classroom for p in tournament.participants.select_related("classroom")],
        )
        services.attach_competition(stage, competition)
        services.complete_stage(stage)
        services.process_advancement(stage)
        services.finalize_tournament(tournament)

        history = services.history_for(tournament)

        self.assertEqual(history["season"], "2025/2026")
        self.assertEqual(len(history["stages"]), 1)
        self.assertEqual(
            history["stages"][0]["competitions"], [competition]
        )
        self.assertEqual(len(history["participants"]), 2)
        self.assertEqual(history["standings"][0]["classroom"], "Science Lab A")

    def test_the_hall_of_fame_lists_only_completed_tournaments(self) -> None:
        finished = create_tournament(name="Finished Cup", season="2025")
        add_participants(finished, "Science Lab A", "Science Lab B")
        stage = services.create_stage(finished, "final")
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=[
            p.classroom for p in finished.participants.select_related("classroom")
        ],
        )
        services.attach_competition(stage, competition)
        services.complete_stage(stage)
        services.process_advancement(stage)
        services.finalize_tournament(finished)

        create_tournament(name="Still Running", season="2026")

        records = services.hall_of_fame()

        self.assertEqual([r.tournament.name for r in records], ["Finished Cup"])

    def test_the_hall_of_fame_keeps_the_winner_name_as_it_was(self) -> None:
        tournament = create_tournament(name="Historic Cup")
        add_participants(tournament, "Science Lab A", "Science Lab B")
        stage = services.create_stage(tournament, "final")
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=[p.classroom for p in tournament.participants.select_related("classroom")],
        )
        services.attach_competition(stage, competition)
        services.complete_stage(stage)
        services.process_advancement(stage)
        services.finalize_tournament(tournament)

        record = services.hall_of_fame()[0]
        participant = tournament.participants.get(classroom__name="Science Lab A")
        Classroom = type(participant.classroom)
        Classroom.objects.filter(pk=participant.classroom_id).update(name="Renamed Lab")

        self.assertEqual(record.winner_name, "Science Lab A")
        self.assertNotEqual(record.winner_name, "Renamed Lab")

    def test_a_record_cannot_be_created_by_hand_through_the_admin(self) -> None:
        """History is produced by finalizing, never typed in."""
        from tournaments.admin import TournamentRecordAdmin

        class Request:
            user = create_administrator()

        self.assertFalse(TournamentRecordAdmin(TournamentRecord, None).has_add_permission(Request()))


class ModelConstraintTests(TestCase):
    """20. Database-level integrity."""

    def test_the_database_rejects_a_duplicate_participant(self) -> None:
        tournament = create_tournament()
        classroom = create_classroom(name="Science Lab A")
        TournamentParticipant.objects.create(
            tournament=tournament, classroom=classroom
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            TournamentParticipant.objects.create(
                tournament=tournament, classroom=classroom
            )

    def test_the_database_rejects_a_zero_advancing_count(self) -> None:
        from django.core.exceptions import ValidationError

        tournament = create_tournament()
        stage = TournamentStage(
            tournament=tournament,
            stage_type=TournamentStageType.QUALIFICATION,
            order=1,
            advancing_count=0,
        )

        with self.assertRaises(ValidationError):
            stage.clean()

    def test_a_tournament_ends_after_it_starts(self) -> None:
        from datetime import date

        from django.core.exceptions import ValidationError

        tournament = Tournament(
            name="Backwards",
            created_by=create_teacher(username="dates"),
            start_date=date(2026, 5, 1),
            end_date=date(2026, 4, 1),
        )

        with self.assertRaises(ValidationError):
            tournament.clean()
