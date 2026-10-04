"""Reports and CSV exports.

Two things are being checked here. First, that the figures are right. Second - and
more importantly - that a report does not compute anything of its own: the
competition report is checked against ``scoring.leaderboard``, the same function
that produces the board shown live in a classroom, so the two cannot drift apart.
"""

from __future__ import annotations

import csv
import io

from django.test import TestCase
from django.urls import reverse

from competitions.models import Competition
from reports import services
from reports.exports import csv_response, slugify_filename
from scoring.leaderboard import leaderboard_rows

from core.tests.utils import (
    create_administrator,
    create_answer,
    create_classroom,
    create_competition,
    create_question,
    create_teacher,
    create_true_false_question,
    played_competition,
)


def parse_csv(response) -> list[list[str]]:
    """Decode a CSV response into rows, dropping the UTF-8 byte-order mark."""
    body = response.content.decode("utf-8")
    if body.startswith("﻿"):
        body = body[1:]
    return list(csv.reader(io.StringIO(body)))


class CsvHelperTests(TestCase):
    def test_none_becomes_an_empty_cell_not_the_word_none(self) -> None:
        response = csv_response("x.csv", ["A", "B"], [[1, None]])

        self.assertEqual(parse_csv(response)[1], ["1", ""])

    def test_the_filename_is_derived_from_the_text_given(self) -> None:
        self.assertEqual(
            slugify_filename("competition", "Science Lab 1!"),
            "competition-Science-Lab-1",
        )

    def test_a_name_of_punctuation_still_produces_a_filename(self) -> None:
        self.assertEqual(slugify_filename("!!!", "###"), "export")

    def test_the_response_is_a_download(self) -> None:
        response = csv_response("report.csv", ["A"], [["1"]])

        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('attachment; filename="report.csv"', response["Content-Disposition"])


class CompetitionReportTests(TestCase):
    """18. Competition reports."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="host")
        self.lab_a = create_classroom(name="Science Lab A")
        self.lab_b = create_classroom(name="Science Lab B")
        self.question = create_question(text="What is 2 + 2?")
        self.competition = create_competition(
            teacher=self.teacher,
            classrooms=[self.lab_a, self.lab_b],
            questions=[self.question],
        )

        # A answers correctly, B answers wrongly.
        create_answer(
            self.competition, self.lab_a, question=self.question,
            points=100, speed_bonus=0, is_correct=True,
        )
        create_answer(
            self.competition, self.lab_b, question=self.question,
            points=0, speed_bonus=0, is_correct=False,
        )

    def test_the_report_totals_are_correct(self) -> None:
        report = services.competition_report(self.competition)

        self.assertEqual(report["classrooms_count"], 2)
        self.assertEqual(report["answers_count"], 2)
        self.assertEqual(report["correct_count"], 1)
        self.assertEqual(report["wrong_count"], 1)
        self.assertEqual(report["accuracy"], 50.0)

    def test_the_report_agrees_with_the_live_leaderboard(self) -> None:
        """The report must not be a second implementation of the same totals."""
        report = services.competition_report(self.competition)
        board = {row.classroom_name: row.score for row in leaderboard_rows(self.competition)}
        from_report = {row["classroom"]: row["score"] for row in report["classrooms"]}

        self.assertEqual(from_report, board)

    def test_classroom_accuracy_is_reported(self) -> None:
        report = services.competition_report(self.competition)
        by_name = {row["classroom"]: row for row in report["classrooms"]}

        self.assertEqual(by_name["Science Lab A"]["accuracy"], 100.0)
        self.assertEqual(by_name["Science Lab B"]["accuracy"], 0.0)

    def test_a_classroom_that_never_answered_still_appears(self) -> None:
        """A school asking "did Lab B take part?" needs a row saying no."""
        silent = create_classroom(name="Science Lab C")
        from competitions.models import CompetitionClassroom

        CompetitionClassroom.objects.create(competition=self.competition, classroom=silent)

        report = services.competition_report(self.competition)

        row = next(r for r in report["classrooms"] if r["classroom"] == "Science Lab C")
        self.assertEqual(row["given"], 0)
        self.assertIsNone(row["accuracy"])

    def test_the_question_breakdown_is_reported(self) -> None:
        report = services.competition_report(self.competition)

        question_row = report["questions"][0]
        self.assertEqual(question_row["question"], "What is 2 + 2?")
        self.assertEqual(question_row["given"], 2)
        self.assertEqual(question_row["correct"], 1)
        self.assertEqual(question_row["accuracy"], 50.0)

    def test_a_round_with_no_answers_reports_no_accuracy_rather_than_zero(self) -> None:
        empty = create_competition(
            teacher=self.teacher,
            classrooms=[self.lab_a],
            questions=[self.question],
        )

        report = services.competition_report(empty)

        self.assertIsNone(report["accuracy"])
        self.assertEqual(report["answers_count"], 0)

    def test_the_report_page_renders_for_the_owning_teacher(self) -> None:
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse("reports:competition", args=[self.competition.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Science Lab A")
        self.assertContains(response, "50.0%")


class ClassroomPerformanceTests(TestCase):
    """Classroom performance across competitions."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="host")
        self.lab = create_classroom(name="Science Lab A")
        # Assigned, so the teacher may report on it - the same rule that lets a
        # teacher see the room on their dashboard.
        self.lab.teachers.add(self.teacher)
        # Two questions per round: one classroom answers once per question, and
        # (competition, question, classroom) is unique.
        self.questions = [create_question(text="Q1"), create_question(text="Q2")]

        self.first = create_competition(
            teacher=self.teacher, title="Round one",
            classrooms=[self.lab], questions=self.questions,
        )
        self.second = create_competition(
            teacher=self.teacher, title="Round two",
            classrooms=[self.lab], questions=self.questions,
        )
        create_answer(self.first, self.lab, question=self.questions[0],
                      points=100, speed_bonus=0, is_correct=True)
        create_answer(self.first, self.lab, question=self.questions[1],
                      points=0, speed_bonus=0, is_correct=False)
        create_answer(self.second, self.lab, question=self.questions[0],
                      points=200, speed_bonus=0, is_correct=True)
        create_answer(self.second, self.lab, question=self.questions[1],
                      points=0, speed_bonus=0, is_correct=False)

    def test_it_counts_the_competitions_played_and_the_total(self) -> None:
        report = services.classroom_performance(self.lab)

        self.assertEqual(report["competitions_played"], 2)
        self.assertEqual(report["total_score"], 300)

    def test_it_reports_the_average_score_per_competition(self) -> None:
        report = services.classroom_performance(self.lab)

        self.assertEqual(report["average_score"], 150.0)

    def test_it_reports_accuracy_across_all_answers(self) -> None:
        report = services.classroom_performance(self.lab)

        self.assertEqual(report["answers_given"], 4)
        self.assertEqual(report["correct_answers"], 2)
        self.assertEqual(report["wrong_answers"], 2)
        self.assertEqual(report["accuracy"], 50.0)

    def test_a_classroom_that_never_played_reports_nothing_rather_than_zero(self) -> None:
        quiet = create_classroom(name="Science Lab Z")

        report = services.classroom_performance(quiet)

        self.assertEqual(report["competitions_played"], 0)
        self.assertIsNone(report["average_score"])
        self.assertIsNone(report["accuracy"])

    def test_the_page_renders(self) -> None:
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("reports:classroom", args=[self.lab.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Science Lab A")


class QuestionStatisticsTests(TestCase):
    """Question statistics: times used, accuracy, response time, type."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="host")
        self.lab = create_classroom(name="Science Lab A")
        # A second room in the second round, so the question can be answered both
        # correctly and wrongly: (competition, question, classroom) is unique.
        self.second_lab = create_classroom(name="Science Lab B")
        self.true_false = create_true_false_question(text="Water boils at 100 C.")

        # Asked in two rounds, answered correctly twice out of three.
        self.first = create_competition(
            teacher=self.teacher, title="One",
            classrooms=[self.lab], questions=[self.true_false],
        )
        self.second = create_competition(
            teacher=self.teacher, title="Two",
            classrooms=[self.lab, self.second_lab], questions=[self.true_false],
        )
        create_answer(self.first, self.lab, question=self.true_false,
                      points=100, speed_bonus=0, is_correct=True)
        create_answer(self.second, self.lab, question=self.true_false,
                      points=100, speed_bonus=0, is_correct=True)
        create_answer(self.second, self.second_lab, question=self.true_false,
                      points=0, speed_bonus=0, is_correct=False)

    def test_it_counts_how_often_the_question_was_used(self) -> None:
        stats = services.question_statistics(self.true_false)

        self.assertEqual(stats["times_used"], 2)
        self.assertEqual(stats["times_asked"], 3)

    def test_it_reports_the_correct_percentage(self) -> None:
        stats = services.question_statistics(self.true_false)

        self.assertEqual(stats["correct_percentage"], 66.7)
        self.assertEqual(stats["correct_answers"], 2)
        self.assertEqual(stats["wrong_answers"], 1)

    def test_it_reports_the_question_type(self) -> None:
        stats = services.question_statistics(self.true_false)

        self.assertEqual(stats["question_type"], "true_false")
        self.assertEqual(stats["question_type_display"], "True or false")

    def test_it_reports_the_average_response_time(self) -> None:
        stats = services.question_statistics(self.true_false)

        self.assertEqual(stats["average_response_seconds"], 5.0)

    def test_an_unused_question_reports_no_accuracy(self) -> None:
        stats = services.question_statistics(create_question())

        self.assertEqual(stats["times_used"], 0)
        self.assertIsNone(stats["correct_percentage"])
        self.assertIsNone(stats["average_response_seconds"])

    def test_the_page_renders(self) -> None:
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse("reports:question", args=[self.true_false.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "True or false")


class TournamentReportTests(TestCase):
    """19. Tournament reports."""

    def setUp(self) -> None:
        from tournaments import services as tournament_services

        self.teacher = create_teacher(username="owner")
        self.tournament = tournament_services.create_tournament(
            name="Primary Championship",
            created_by=self.teacher,
            season="2026",
        )
        from core.tests.utils import add_participants

        participants = add_participants(
            self.tournament, "Science Lab A", "Science Lab B"
        )
        stage = tournament_services.create_stage(self.tournament, "final")
        self.competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=[p.classroom for p in participants],
        )
        tournament_services.attach_competition(stage, self.competition)
        tournament_services.complete_stage(stage)
        tournament_services.process_advancement(stage)
        tournament_services.finalize_tournament(self.tournament)

    def test_the_report_names_the_winner_and_the_season(self) -> None:
        report = services.tournament_report(self.tournament)

        self.assertEqual(report["winner"], "Science Lab A")
        self.assertEqual(report["season"], "2026")

    def test_the_report_lists_the_stages_and_their_competitions(self) -> None:
        report = services.tournament_report(self.tournament)

        self.assertEqual(len(report["stages"]), 1)
        self.assertEqual(
            report["stages"][0]["competitions"][0]["competition"], self.competition
        )

    def test_the_report_reuses_the_competition_report(self) -> None:
        """One definition, so a tournament and a round cannot disagree."""
        report = services.tournament_report(self.tournament)
        competition_report = services.competition_report(self.competition)

        self.assertEqual(
            report["stages"][0]["competitions"][0]["classrooms"],
            competition_report["classrooms"],
        )

    def test_the_report_lists_the_finalists(self) -> None:
        report = services.tournament_report(self.tournament)

        self.assertEqual(
            [row["classroom"] for row in report["finalists"]],
            ["Science Lab A", "Science Lab B"],
        )

    def test_the_page_renders_for_the_owner(self) -> None:
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse("reports:tournament", args=[self.tournament.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Primary Championship")
        self.assertContains(response, "Science Lab A")


class CsvExportTests(TestCase):
    """20. CSV exports."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="host")
        self.lab_a = create_classroom(name="Science Lab A")
        self.lab_b = create_classroom(name="Science Lab B")
        # Assigned so the teacher may report on them.
        self.lab_a.teachers.add(self.teacher)
        self.lab_b.teachers.add(self.teacher)
        self.question = create_question()
        self.competition = create_competition(
            teacher=self.teacher,
            classrooms=[self.lab_a, self.lab_b],
            questions=[self.question],
        )
        create_answer(self.competition, self.lab_a, question=self.question,
                      points=100, speed_bonus=0, is_correct=True)
        create_answer(self.competition, self.lab_b, question=self.question,
                      points=0, speed_bonus=0, is_correct=False)
        self.client.force_login(self.teacher)

    def test_the_competition_export_matches_the_report(self) -> None:
        response = self.client.get(
            reverse("reports:competition_csv", args=[self.competition.pk])
        )

        self.assertEqual(response.status_code, 200)
        rows = parse_csv(response)
        report = services.competition_report(self.competition)

        self.assertEqual(rows[0][0], "Classroom")
        self.assertEqual(rows[0][5], "Final score")
        self.assertEqual(len(rows) - 1, len(report["classrooms"]))
        self.assertEqual(rows[1][0], "Science Lab A")
        self.assertEqual(rows[1][5], "100")
        self.assertEqual(rows[2][0], "Science Lab B")
        self.assertEqual(rows[2][5], "0")

    def test_the_export_is_offered_as_a_download(self) -> None:
        response = self.client.get(
            reverse("reports:competition_csv", args=[self.competition.pk])
        )

        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("text/csv", response["Content-Type"])

    def test_the_classroom_export_reports_the_totals(self) -> None:
        response = self.client.get(reverse("reports:classroom_csv", args=[self.lab_a.pk]))

        rows = parse_csv(response)
        self.assertEqual(rows[1][0], "Science Lab A")
        self.assertEqual(rows[1][2], "1")   # answers given
        self.assertEqual(rows[1][6], "100")  # total score

    def test_the_export_escapes_a_classroom_name_with_a_comma(self) -> None:
        awkward = create_classroom(name="Lab, West")
        awkward.teachers.add(self.teacher)
        other = create_competition(
            teacher=self.teacher, title="Awkward",
            classrooms=[awkward], questions=[self.question],
        )
        create_answer(other, awkward, question=self.question,
                      points=50, speed_bonus=0, is_correct=True)

        response = self.client.get(reverse("reports:classroom_csv", args=[awkward.pk]))

        rows = parse_csv(response)
        self.assertEqual(rows[1][0], "Lab, West")

    def test_the_tournament_export_lists_stages_and_the_final_result(self) -> None:
        from tournaments import services as tournament_services

        tournament = tournament_services.create_tournament(
            name="Export Cup", created_by=self.teacher
        )
        # The classrooms already made in setUp, rather than new ones with the same
        # names: a classroom name is unique across the school.
        from tournaments.services import add_participant

        for classroom in (self.lab_a, self.lab_b):
            add_participant(tournament, classroom)
        stage = tournament_services.create_stage(tournament, "final")
        competition = played_competition(
            scores={"Science Lab A": (300, 0), "Science Lab B": (250, 0)},
            classrooms=[self.lab_a, self.lab_b],
        )
        tournament_services.attach_competition(stage, competition)
        tournament_services.complete_stage(stage)
        tournament_services.process_advancement(stage)
        tournament_services.finalize_tournament(tournament)

        response = self.client.get(
            reverse("reports:tournament_csv", args=[tournament.pk])
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Export Cup", body)
        self.assertIn("Science Lab A: 300 pts", body)


class ReportAccessTests(TestCase):
    """A report is never a wider door than the pages around it."""

    def setUp(self) -> None:
        self.teacher = create_teacher(username="owner")
        self.other = create_teacher(username="other")
        self.competition = create_competition(teacher=self.teacher)

    def test_an_anonymous_visitor_is_sent_to_sign_in(self) -> None:
        for name in ("reports:index",):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))

                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_another_teacher_cannot_report_on_someone_elses_competition(self) -> None:
        self.client.force_login(self.other)

        response = self.client.get(
            reverse("reports:competition", args=[self.competition.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_another_teacher_cannot_export_someone_elses_competition(self) -> None:
        self.client.force_login(self.other)

        response = self.client.get(
            reverse("reports:competition_csv", args=[self.competition.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_a_teacher_cannot_export_a_classroom_that_is_not_theirs(self) -> None:
        foreign = create_classroom(name="Not Mine")
        self.client.force_login(self.other)

        response = self.client.get(reverse("reports:classroom_csv", args=[foreign.pk]))

        self.assertEqual(response.status_code, 404)

    def test_an_administrator_may_report_on_any_competition(self) -> None:
        self.client.force_login(create_administrator())

        response = self.client.get(
            reverse("reports:competition", args=[self.competition.pk])
        )

        self.assertEqual(response.status_code, 200)
