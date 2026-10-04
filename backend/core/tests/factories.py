"""Object factories for tests.

Deliberately thin: each helper sets only the fields a test cares about, so a
test never fails because an unrelated field gained a validator.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.roles import Role

UserModel = get_user_model()

# Long enough to satisfy the default password validators without being cryptic.
TEST_PASSWORD = "test-password-not-a-real-secret"

# Distinguishes "caller did not pass this" from "caller passed an empty list".
_UNSET = object()


def create_user(username: str = "staff", role: str = Role.TEACHER, **extra):
    """Create a signed-in-able staff account."""
    extra.setdefault("full_name", username.replace(".", " ").title())
    return UserModel.objects.create_user(
        username=username, password=TEST_PASSWORD, role=role, **extra
    )


def create_administrator(username: str = "administrator", **extra):
    return create_user(username=username, role=Role.ADMINISTRATOR, **extra)


def create_teacher(username: str = "teacher", **extra):
    return create_user(username=username, role=Role.TEACHER, **extra)


def create_classroom(name: str = "Science Lab 1", **extra):
    from classrooms.models import Classroom

    return Classroom.objects.create(name=name, **extra)


def create_screen(name: str = "Front board", **extra):
    from screens.models import InteractiveScreen

    return InteractiveScreen.objects.create(name=name, **extra)


def create_question(text: str = "What is 2 + 2?", **extra):
    from questions.models import Question, QuestionType

    extra.setdefault("question_type", QuestionType.MULTIPLE_CHOICE)
    extra.setdefault("options", ["3", "4", "5", "6"])
    # A scorable question by default. Phase 5 scores answers, so a fixture
    # without a key would quietly produce unscorable questions and make every
    # scoring test fail for a reason that has nothing to do with scoring.
    extra.setdefault("correct_option", extra["options"][1])
    return Question.objects.create(text=text, **extra)


def create_true_false_question(text: str = "Water boils at 100 °C.", value: bool = True, **extra):
    """A true/false question, keyed so it can be scored like any other."""
    from questions.models import Question, QuestionType

    extra.setdefault("question_type", QuestionType.TRUE_FALSE)
    extra.setdefault("correct_answer", value)
    return Question.objects.create(text=text, **extra)


def create_ordering_question(items=None, correct_order=None, **extra):
    """An ordering question, keyed and scorable.

    The two orderings are separate arguments rather than one, because the whole
    point of the type is that the items may be presented in an order that is not
    the answer.
    """
    from questions.models import Question, QuestionType

    extra.setdefault("question_type", QuestionType.ORDERING)
    extra.setdefault("options", [])
    extra.setdefault("correct_option", "")
    if correct_order is not None:
        extra.setdefault(
            "type_config", {"items": list(items or []), "correct_order": list(correct_order)}
        )
    return Question.objects.create(text=extra.pop("text", "Put these in order."), **extra)


def create_classification_question(categories=None, assignments=None, **extra):
    """A classification question, keyed and scorable.

    ``assignments`` is a list of ``(label, category)`` pairs, which reads better
    at a call site than the dict-of-dicts that gets stored.
    """
    from questions.models import Question, QuestionType

    extra.setdefault("question_type", QuestionType.CLASSIFICATION)
    extra.setdefault("options", [])
    extra.setdefault("correct_option", "")
    if assignments is not None:
        extra.setdefault(
            "type_config",
            {
                "categories": list(categories or []),
                "assignments": [
                    {"label": label, "category": category}
                    for label, category in assignments
                ],
            },
        )
    return Question.objects.create(
        text=extra.pop("text", "Place each item."), **extra
    )


def create_short_answer_question(accepted_answers=None, case_sensitive: bool = False, **extra):
    """A short answer question, keyed and scorable."""
    from questions.models import Question, QuestionType

    extra.setdefault("question_type", QuestionType.SHORT_ANSWER)
    extra.setdefault("options", [])
    extra.setdefault("correct_option", "")
    if accepted_answers is not None:
        extra.setdefault(
            "type_config",
            {
                "accepted_answers": list(accepted_answers),
                "case_sensitive": case_sensitive,
            },
        )
    return Question.objects.create(text=extra.pop("text", "Type your answer."), **extra)


def create_competition(teacher=None, title: str = "Science Lab 1 versus Lab 2", **extra):
    """Create a competition, with one classroom and one question by default.

    Defaults keep live tests to a single line of setup while still producing a
    competition that :func:`live.services.start_competition` will accept.

    ``classrooms`` and ``questions`` are consumed here as related rows rather than
    passed to the model constructor, which has no such fields.
    """
    from competitions.models import Competition, CompetitionClassroom, CompetitionQuestion

    if teacher is None:
        teacher = create_teacher(username="host.teacher")

    # A sentinel rather than a plain default, because an explicitly empty list
    # is a meaningful case here: "a competition with no classrooms" must build a
    # competition with none, not silently fall back to the default set.
    classrooms = extra.pop("classrooms", _UNSET)
    questions = extra.pop("questions", _UNSET)

    if classrooms is _UNSET:
        classrooms = [create_classroom(name="Science Lab 1")]
    if questions is _UNSET:
        questions = [create_question()]

    competition = Competition.objects.create(title=title, teacher=teacher, **extra)

    for classroom in classrooms:
        CompetitionClassroom.objects.create(
            competition=competition, classroom=classroom
        )
    for index, question in enumerate(questions, start=1):
        CompetitionQuestion.objects.create(
            competition=competition, question=question, position=index
        )
    return competition


def include_classroom(competition, classroom):
    from competitions.models import CompetitionClassroom

    entry, _ = CompetitionClassroom.objects.get_or_create(
        competition=competition, classroom=classroom
    )
    return entry


def add_question(competition, question=None, position: int = 1, **extra):
    from competitions.models import CompetitionQuestion

    if question is None:
        question = create_question()
    return CompetitionQuestion.objects.create(
        competition=competition,
        question=question,
        position=position,
        **extra,
    )


def create_scoring_rule(competition, **extra):
    """A per-competition scoring rule. Without one the configured defaults apply."""
    from scoring.models import ScoringRule

    return ScoringRule.objects.create(competition=competition, **extra)


def create_answer(competition, classroom, question=None, **extra):
    """An answer row, bypassing the service.

    For tests that need a stored answer as a starting point - a leaderboard with
    several questions already scored, say - rather than as the thing under test.
    Tests about submission use ``scoring.services.submit_answer`` instead, so the
    rules they claim to check are the ones actually exercised.
    """
    from scoring.models import Answer

    question = question or competition.current_question
    position = extra.pop(
        "position",
        competition.questions.filter(question=question).first().position,
    )
    extra.setdefault("selected_answer", question.answer_key)
    # Default to when the question was actually put on screen. A competition that
    # has never been started has no such time, and falling back to "now" keeps the
    # row valid instead of writing a null into a non-null column - which is what
    # makes a standings or report fixture possible without first driving the whole
    # live engine.
    extra.setdefault(
        "submitted_at", competition.current_question_started_at or timezone.now()
    )
    extra.setdefault("response_time_seconds", 5.0)
    extra.setdefault("duration_seconds", 30)
    extra.setdefault("is_correct", True)
    extra.setdefault("points", 100)
    extra.setdefault("speed_bonus", 42)
    extra.setdefault("correct_answer", question.answer_key)

    return Answer.objects.create(
        competition=competition,
        question=question,
        position=position,
        classroom=classroom,
        **extra,
    )


# ---------------------------------------------------------------------------
# A competition that has already been played
# ---------------------------------------------------------------------------
#
# Championship tests need rounds that are genuinely over: finished state, answers
# stored, and a frozen CompetitionResult. Building that by driving the live engine
# would make every one of those tests a slow integration test of Phase 6 as well,
# and would fail for reasons that have nothing to do with tournaments. This builds
# the same facts directly, using the same scoring services that do it in
# production.


def complete_competition(competition, scores, *, question=None, answers_correct=True):
    """Finish ``competition`` with the given per-classroom scores.

    ``scores`` maps a classroom to ``(points, speed_bonus)``, or to
    ``(points, speed_bonus, is_correct)`` where the correctness has to differ from
    "scored something". Scores are written as stored answers and the round's final
    standings are frozen with :func:`scoring.services.finalise`, so the resulting
    data is indistinguishable from a round a teacher actually ran.
    """
    from django.utils import timezone

    from competitions.models import CompetitionState
    from scoring import services as scoring_services

    if question is None:
        question = competition.questions.order_by("position").first().question
    entry = competition.questions.filter(question=question).first()

    for classroom, figures in scores.items():
        points, bonus = int(figures[0]), int(figures[1])
        is_correct = bool(figures[2]) if len(figures) > 2 else bool(points)
        create_answer(
            competition,
            classroom,
            question=question,
            position=entry.position,
            points=points,
            speed_bonus=bonus,
            is_correct=is_correct,
            submitted_at=timezone.now(),
            response_time_seconds=5.0,
            duration_seconds=30,
        )

    competition.state = CompetitionState.FINISHED
    competition.finished_at = timezone.now()
    competition.total_questions = max(
        competition.total_questions, competition.questions.count()
    )
    competition.save(
        update_fields=["state", "finished_at", "total_questions", "updated_at"]
    )
    scoring_services.finalise(competition)
    return competition


def played_competition(
    teacher=None, scores=None, *, classrooms=None, title=None, questions=None, **extra
):
    """A finished competition whose results are frozen.

    ``scores`` is keyed by classroom *name* when ``classrooms`` is not given, so a
    championship test can read as a list of results rather than as setup::

        played_competition(scores={"Lab A": (300, 0), "Lab B": (250, 0)})
    """
    if classrooms is None:
        if scores is None:
            classrooms = [create_classroom(name="Science Lab 1")]
            scores = {classrooms[0]: (100, 0)}
        else:
            # Keyed by name for readability: a championship test should read as a
            # list of results, not as a page of setup.
            classrooms = [create_classroom(name=name) for name in scores]

    if scores and all(isinstance(key, str) for key in scores):
        by_name = {classroom.name: classroom for classroom in classrooms}
        scores = {by_name[name]: figures for name, figures in scores.items()}

    if teacher is None:
        # A distinct owner per round, because a championship test builds several
        # rounds in one test method and usernames are unique.
        _played_competition_counter[0] += 1
        teacher = create_teacher(username=f"played.round.{_played_competition_counter[0]}")

    competition = create_competition(
        teacher=teacher,
        title=title or "A played round",
        classrooms=classrooms,
        **({"questions": questions} if questions is not None else {}),
        **extra,
    )
    return complete_competition(competition, scores)


# ---------------------------------------------------------------------------
# Tournaments
# ---------------------------------------------------------------------------


def create_tournament(created_by=None, **extra):
    """A tournament in ``draft``, with no stages and no participants.

    Created through :func:`tournaments.services.create_tournament` so that a test
    fixture and the application agree on what a valid tournament is.

    The default owner gets a fresh username each call: a test that builds three
    tournaments is testing three tournaments, not colliding on one account.
    """
    from tournaments import services as tournament_services

    if created_by is None:
        _tournament_owner_counter[0] += 1
        created_by = create_teacher(
            username=f"tournament.host.{_tournament_owner_counter[0]}"
        )

    extra.setdefault("name", "Primary Championship")
    extra.setdefault("season", "2026")

    return tournament_services.create_tournament(created_by=created_by, **extra)


_tournament_owner_counter = [0]
_played_competition_counter = [0]


def add_participants(tournament, *names):
    """Enter several classrooms by name, returning the participant rows."""
    from tournaments import services as tournament_services

    participants = []
    for name in names:
        classroom = create_classroom(name=name)
        participants.append(tournament_services.add_participant(tournament, classroom))
    return participants


def create_stage(tournament, stage_type="qualification", **extra):
    from tournaments import services as tournament_services

    return tournament_services.create_stage(tournament, stage_type, **extra)


def attach_competition(stage, competition):
    from tournaments import services as tournament_services

    return tournament_services.attach_competition(stage, competition)