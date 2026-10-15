"""Competition wizard for the teacher workspace.

A multi-step guided wizard that lets a teacher create a competition without
seeing Django model internals, and without having to finish it in one sitting.

The order is the order a teacher thinks in:

1. **basic** - the competition's name and details.
2. **subject_classrooms** - the subject the questions come from, and the
   classrooms that take part. The subject is chosen here, before the questions,
   because it is what narrows the question bank in step 3.
3. **questions** - picking questions from that subject's bank.
4. **settings** - timing and scoring, chosen once the questions are known.
5. **review** - everything, before it is saved.

The wizard stores its temporary state in the user's session, so it survives
leaving the page and coming back later - which is what makes "prepare this
today, start it on Thursday" possible. On final submission the existing
Competition engine is used, so nothing is duplicated and the live engine is not
touched. Saving a draft creates a competition in the **not-started** state: it is
never live merely because it was saved.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.http.request import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from urllib.parse import urlencode
from django.views.generic import View

from accounts.permissions import is_administrator, is_staff_member
from classrooms.models import Classroom
from competitions.models import Competition, CompetitionClassroom, CompetitionQuestion, CompetitionState
from questions.models import Question, QuestionType
from screens import targeting as screen_targeting
from subjects.models import Subject
from teacher.tournament_wizard import COMPETITION_RETURN_KEY

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wizard step constants
# ---------------------------------------------------------------------------

STEP_BASIC = "basic"
STEP_SUBJECT_CLASSROOMS = "subject_classrooms"
STEP_QUESTIONS = "questions"
STEP_SETTINGS = "settings"
STEP_REVIEW = "review"

STEP_ORDER = [
    STEP_BASIC,
    STEP_SUBJECT_CLASSROOMS,
    STEP_QUESTIONS,
    STEP_SETTINGS,
    STEP_REVIEW,
]

STEP_LABELS = {
    STEP_BASIC: _("1 Basic Info"),
    STEP_SUBJECT_CLASSROOMS: _("2 Subject and Classrooms"),
    STEP_QUESTIONS: _("3 Questions"),
    STEP_SETTINGS: _("4 Settings"),
    STEP_REVIEW: _("5 Review"),
}

SESSION_KEY = "teacher_competition_wizard"


def _wizard_data(request: HttpRequest) -> dict:
    """Return the wizard's session data, initialising if missing."""
    data = request.session.get(SESSION_KEY, {})
    if "step" not in data:
        data["step"] = STEP_BASIC
    if "basic" not in data:
        data["basic"] = {"title": "", "subject": "", "grade": "", "description": ""}
    if "classroom_ids" not in data:
        data["classroom_ids"] = []
    if "subject_id" not in data:
        data["subject_id"] = ""
    if "topic_id" not in data:
        data["topic_id"] = ""
    if "settings" not in data:
        from scoring.models import DEFAULT_CORRECT_POINTS, DEFAULT_SPEED_BONUS_POINTS

        data["settings"] = {
            "duration_seconds": 30,
            "scoring_mode": "standard",
            "speed_bonus": DEFAULT_SPEED_BONUS_POINTS,
            "points_per_question": DEFAULT_CORRECT_POINTS,
            "planned_for": "",
        }
    if "question_ids" not in data:
        data["question_ids"] = []
    if "question_points" not in data:
        data["question_points"] = {}
    return data


def _save_wizard(request: HttpRequest, data: dict) -> None:
    """Persist wizard data to the session."""
    request.session[SESSION_KEY] = data


def _clear_wizard(request: HttpRequest) -> None:
    """Remove wizard data from the session."""
    request.session.pop(SESSION_KEY, None)


def _classrooms_for_user(user) -> list:
    """Classrooms the user may assign to a competition."""
    if is_administrator(user):
        return list(Classroom.objects.filter(active=True).order_by("name"))
    return list(
        Classroom.objects.filter(teachers=user, active=True).order_by("name")
    )


def _authorised_classroom_ids(user) -> set[str]:
    """The string ids of the classrooms this user may include in a round.

    One lookup, one definition of "authorised", used by both the step-2 form
    handler and the save path. The form only *offers* these ids; this is what
    stops a hand-crafted POST naming a classroom the teacher was never assigned
    from reaching ``CompetitionClassroom`` - which would otherwise put a room the
    teacher does not run in front of that room's screen, and would let one
    teacher drive another teacher's class. Rendering fewer checkboxes is not a
    permission check, so the same set is re-applied server-side on save.
    """
    return {str(classroom.pk) for classroom in _classrooms_for_user(user)}


def _questions_for_user(
    user,
    q: str = "",
    question_type: str = "",
    subject_id: str | int = "",
    topic_id: str | int = "",
) -> list:
    """Questions a teacher may select from, filtered by what they asked for.

    The subject is the wizard's organising choice, so it is applied *here* and
    not left to the template: a question from another subject cannot silently
    enter a round just because the teacher scrolled or filtered. An empty
    subject means "the bank as a whole", which is what the wizard shows before
    step 2 has been completed.

    Topics narrow within the subject, and are ignored when the subject is
    unknown - a topic belongs to exactly one subject, so there is nothing to
    narrow inside.
    """
    qs = Question.objects.filter(active=True).order_by("created_at")

    subject_pk = str(subject_id or "").strip()
    if subject_pk.isdigit() and Subject.objects.filter(pk=subject_pk, active=True).exists():
        qs = qs.filter(subject_id=subject_pk)

        topic_pk = str(topic_id or "").strip()
        if topic_pk.isdigit():
            qs = qs.filter(topic_id=topic_pk)

    if q:
        qs = qs.filter(text__icontains=q)
    if question_type:
        qs = qs.filter(question_type=question_type)
    return list(qs)


def _filter_questions_for_user(user, q: str = "", question_type: str = "") -> list:
    """Return questions not already selected, filtered by q and question_type."""
    return [
        qn
        for qn in _questions_for_user(user, q, question_type)
        if qn not in user.__dict__.get("_selected_questions", [])
    ]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _override_points_for(question_points: dict, question_id) -> int | None:
    """The per-question override for one question, or ``None`` for "use default".

    Written as an explicit ``None`` check rather than ``a or b`` on purpose. A
    ``0`` is falsy, so the ``or`` form silently discarded it and fell back to the
    round's default: the review row then showed the default, the review total
    added the ``0``, and the save wrote the default again - three different
    answers for the same question. The session stores overrides keyed by string
    and lookups arrive as either, so both spellings are tried and a real ``0`` is
    honoured as what it is: an invalid value that validation rejects elsewhere.
    """
    if not isinstance(question_points, dict):
        return None
    for key in (str(question_id), question_id):
        if key in question_points:
            return question_points[key]
    try:
        return question_points.get(int(question_id))
    except (TypeError, ValueError):
        return None


def _point_override_errors(question_ids, question_points: dict) -> dict[str, str]:
    """Reject any stored per-question override that is not a usable point value.

    The questions step validates what the teacher typed, and this re-checks the
    values the review and save paths are about to act on. It matters because the
    wizard's state lives in the session: a value that was valid when it was
    entered can be stale, hand-edited or truncated by the time the round is
    saved, and a zero-point or negative question would quietly change the total
    every screen and the leaderboard then worked from.
    """
    from scoring.models import MAX_CORRECT_POINTS

    errors: dict[str, str] = {}
    if not isinstance(question_points, dict):
        return errors

    for qid in question_ids:
        value = _override_points_for(question_points, qid)
        if value is None:
            continue
        try:
            points = int(value)
        except (TypeError, ValueError):
            errors[f"points_{qid}"] = _("Enter a whole number of points.")
            continue
        if points < 1 or points > MAX_CORRECT_POINTS:
            errors[f"points_{qid}"] = _(
                "Points must be between 1 and %d."
            ) % MAX_CORRECT_POINTS
    return errors


def _validate_basic(data: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    title = (data.get("title") or "").strip()
    if not title:
        errors["title"] = _("A competition name is required.")
    return errors


def _validate_classrooms(classroom_ids: list) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not classroom_ids:
        errors["classrooms"] = _("Select at least one classroom.")
    return errors


def _validate_subject_and_classrooms(data: dict) -> dict[str, str]:
    """Step 2 holds two choices and both have to be made.

    The subject is required because it is what narrows the bank; leaving it empty
    would let a round be built from every subject in the school by accident.
    """
    errors: dict[str, str] = {}

    subject_id = str(data.get("subject_id") or "").strip()
    if not subject_id.isdigit() or not Subject.objects.filter(pk=subject_id).exists():
        errors["subject"] = _("Choose the subject these questions come from.")
    elif not Question.objects.filter(subject_id=subject_id, active=True).exists():
        errors["subject"] = _("There are no questions in this subject yet.")

    errors.update(_validate_classrooms(data.get("classroom_ids", [])))
    return errors


def _validate_settings(settings: dict) -> dict[str, str]:
    from scoring.models import DEFAULT_CORRECT_POINTS, MAX_CORRECT_POINTS, MAX_SPEED_BONUS_POINTS

    errors: dict[str, str] = {}
    try:
        duration = int(settings.get("duration_seconds", 0))
        if duration < 5 or duration > 600:
            errors["duration_seconds"] = _("Time per question must be between 5 and 600 seconds.")
    except (ValueError, TypeError):
        errors["duration_seconds"] = _("Enter a valid number of seconds.")

    try:
        points_per_question = int(settings.get("points_per_question", DEFAULT_CORRECT_POINTS))
        if points_per_question < 1 or points_per_question > MAX_CORRECT_POINTS:
            errors["points_per_question"] = _(
                "Points per question must be between 1 and %d."
            ) % MAX_CORRECT_POINTS
    except (ValueError, TypeError):
        errors["points_per_question"] = _("Enter a valid number of points.")

    try:
        speed_bonus = int(settings.get("speed_bonus", 0))
        if speed_bonus < 0 or speed_bonus > MAX_SPEED_BONUS_POINTS:
            errors["speed_bonus"] = _(
                "The speed bonus cannot be negative or more than %d points."
            ) % MAX_SPEED_BONUS_POINTS
    except (ValueError, TypeError):
        errors["speed_bonus"] = _("Enter a valid speed bonus.")

    planned = (settings.get("planned_for") or "").strip()
    if planned:
        from django.utils import timezone
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(planned)
        if parsed is None:
            errors["planned_for"] = _("Enter the date and time in the format YYYY-MM-DD HH:MM.")
        else:
            if timezone.is_naive(parsed):
                parsed = parsed.replace(tzinfo=timezone.get_current_timezone())
            if parsed < timezone.now():
                errors["planned_for"] = _("Choose a time in the future.")

    return errors


def _validate_questions(question_ids: list) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not question_ids:
        errors["questions"] = _("Select at least one question.")
    return errors


# ---------------------------------------------------------------------------
# View: wizard dispatcher
# ---------------------------------------------------------------------------

class CompetitionWizardView(View):
    """Multi-step competition creation wizard."""

    template_name = "teacher/wizard.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_staff_member(request.user):
            raise Http404("This account has no assigned role.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        data = _wizard_data(request)
        step = data["step"]

        # A question just created in the Question Builder and sent back here.
        # The wizard's own state is in the session, so only the new question has
        # to be joined to it - which is what makes the round trip safe: the
        # teacher leaves at the questions step with a name, settings, classrooms
        # and earlier choices already saved, and comes back to exactly that.
        returned_question = self._adopt_returned_question(request, data)
        if returned_question is not None:
            return returned_question

        # Question search/filter only changes results, never the wizard state.
        if step == STEP_QUESTIONS:
            q = request.GET.get("q", "")
            question_type = request.GET.get("type", "")
            data["filters"] = {"q": q, "type": question_type}
            _save_wizard(request, data)

        context = self._common_context(request, data, step)
        context.update(self._step_context(request, data, step))
        return render(request, self.template_name, context)

    @staticmethod
    def _adopt_returned_question(request: HttpRequest, data: dict):
        """Select a question the teacher created while away, and return.

        ``teacher/questions/create/?save_to_competition=1`` sends the teacher back
        to ``?question_id=<pk>``. The new question is added to the selection, the
        filters are cleared - it may not match the search that was on screen -
        and a message confirms it, so the teacher lands on the questions step with
        the question they just made ticked and selectable.

        Returns a redirect, or ``None`` when there is nothing to adopt, so an
        ordinary visit to the wizard is untouched.
        """
        raw_id = (request.GET.get("question_id") or "").strip()
        if not raw_id.isdigit():
            if raw_id:
                # A malformed id is a bug or a mistyped link, not something to
                # fail on: ignore it rather than raising a 500 on the wizard.
                logger.info("Wizard returned an unusable question id %r.", raw_id)
            return None

        question = Question.objects.filter(pk=raw_id).first()
        if question is None:
            messages.info(
                request,
                _("The question you created is no longer available."),
            )
            return redirect("teacher:create_competition")

        selected = [str(qid) for qid in data.get("question_ids", [])]
        if str(question.pk) not in selected:
            selected.append(str(question.pk))
        data["question_ids"] = selected
        data["filters"] = {"q": "", "type": ""}
        _save_wizard(request, data)
        messages.success(
            request,
            _('Question "%(text)s" added to this competition.')
            % {"text": question.text[:60]},
        )
        return redirect("teacher:create_competition")

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        data = _wizard_data(request)
        step = data["step"]
        action = request.POST.get("action", step)

        # Rejected per-question point values for this submission, collected on the
        # questions step and checked after the step data is persisted.
        point_errors: dict[str, str] = {}

        # Persist POST data for the current step before acting
        if step == STEP_BASIC:
            data["basic"] = {
                "title": request.POST.get("title", ""),
                "subject": request.POST.get("subject", ""),
                "grade": request.POST.get("grade", ""),
                "description": request.POST.get("description", ""),
            }
        elif step == STEP_SUBJECT_CLASSROOMS:
            data["subject_id"] = (request.POST.get("subject_id") or "").strip()
            data["topic_id"] = (request.POST.get("topic_id") or "").strip()
            # Re-applied here, not just in the form: the checkboxes are a
            # convenience, and a POST naming a classroom the teacher was never
            # assigned to is refused rather than stored. See
            # _authorised_classroom_ids.
            allowed = _authorised_classroom_ids(request.user)
            requested = request.POST.getlist("classroom_ids")
            rejected = [cid for cid in requested if str(cid) not in allowed]
            data["classroom_ids"] = [
                cid for cid in dict.fromkeys(requested) if str(cid) in allowed
            ]
            if rejected:
                logger.info(
                    "Teacher %s submitted %d unauthorised classroom id(s).",
                    request.user.pk,
                    len(rejected),
                )
                messages.error(
                    request,
                    _("Some of those classrooms are not assigned to you and were not added."),
                )
        elif step == STEP_SETTINGS:
            # Update points_per_question and planned_for in settings too
            from scoring.models import DEFAULT_CORRECT_POINTS, DEFAULT_SPEED_BONUS_POINTS

            data["settings"].update(
                {
                    "duration_seconds": int(request.POST.get("duration_seconds", 30)) if request.POST.get("duration_seconds") else 30,
                    "scoring_mode": request.POST.get("scoring_mode", "standard"),
                    "speed_bonus": int(request.POST.get("speed_bonus", DEFAULT_SPEED_BONUS_POINTS)) if request.POST.get("speed_bonus") else DEFAULT_SPEED_BONUS_POINTS,
                    "points_per_question": int(request.POST.get("points_per_question", DEFAULT_CORRECT_POINTS)) if request.POST.get("points_per_question") else DEFAULT_CORRECT_POINTS,
                    "planned_for": (request.POST.get("planned_for") or "").strip(),
                }
            )
        elif step == STEP_QUESTIONS:
            from scoring.models import MAX_CORRECT_POINTS

            picked = request.POST.getlist("question_ids")
            # A question chosen before the subject was set can belong to another
            # subject. Drop it rather than letting it ride along into a round it
            # was never intended for - which is the "silently included" case the
            # workflow has to never allow.
            if action == "next" and data.get("subject_id"):
                allowed = {
                    str(q.pk) for q in _questions_for_user(request.user, subject_id=data["subject_id"])
                }
                rejected = [qid for qid in picked if qid not in allowed]
                if rejected:
                    picked = [qid for qid in picked if qid in allowed]
                    messages.info(
                        request,
                        _("Questions from other subjects were not added."),
                    )
            data["question_ids"] = picked
            # Persist per-question point overrides, rejecting invalid values
            # (blank is fine and means "use the default"; anything else against
            # the same rules as the round default). A dropped-in-the-silence
            # override is exactly the "server did not compute the total" case.
            question_points: dict[str, int] = {}
            for qid in picked:
                key = f"points_{qid}"
                raw = request.POST.get(key)
                if raw is None or raw.strip() == "":
                    continue
                try:
                    value = int(raw.strip())
                except (TypeError, ValueError):
                    point_errors[key] = _("Enter a whole number of points.")
                    continue
                if value < 1 or value > MAX_CORRECT_POINTS:
                    point_errors[key] = _(
                        "Points must be between 1 and %d."
                    ) % MAX_CORRECT_POINTS
                    continue
                question_points[str(qid)] = value
            data["question_points"] = question_points
        _save_wizard(request, data)

        # A bad per-question value is only an error on the actions that would
        # carry it forward; filtering and reordering leave it to the next pass.
        if point_errors and action in ("next", "save_draft"):
            context = self._common_context(request, data, step)
            context["errors"] = point_errors
            context.update(self._step_context(request, data, step))
            return render(request, self.template_name, context)

        if action == "move_up":
            return self._move_question(request, data, "up")
        if action == "move_down":
            return self._move_question(request, data, "down")

        # Handle step-specific actions
        if action == "cancel":
            _clear_wizard(request)
            messages.info(request, _("Competition creation cancelled."))
            if request.session.pop(COMPETITION_RETURN_KEY, None):
                return redirect("teacher:create_tournament")
            return redirect("teacher:dashboard")

        if action == "add_random":
            return self._add_random_questions(request, data)

        if action == "save_draft":
            return self._save_draft(request, data)

        if action == "next":
            return self._advance(request, data, step)

        if action == "back":
            return self._retreat(request, data, step)

        if action == "submit":
            return self._create_competition(request, data)

        # Unknown action – stay on current step
        messages.error(request, _("Unknown action: %(action)s.") % {"action": action})
        return self.get(request)

    def _add_random_questions(self, request: HttpRequest, data: dict) -> HttpResponse:
        """Pick a number of questions at random from the chosen subject's bank.

        The draw happens here, once, and the ids are written into the wizard's
        state - so the saved competition holds the questions that were actually
        drawn rather than a rule that would draw a *different* set every time the
        detail page is opened. That is what makes "choose 10 questions from
        Science" reproducible.
        """
        requested = (request.POST.get("random_count") or "").strip()
        try:
            count = int(requested)
        except (TypeError, ValueError):
            count = 0

        bank = _questions_for_user(request.user, subject_id=data.get("subject_id", ""))

        if count < 1:
            messages.error(request, _("Enter how many questions to pick."))
            return self.get(request)

        if not bank:
            messages.error(
                request,
                _("There are no questions in this subject yet."),
            )
            return self.get(request)

        if count > len(bank):
            count = len(bank)
            messages.info(
                request,
                _("Only %(available)d questions are available in this subject, so %(available)d were selected.")
                % {"available": count},
            )

        # `order_by("?")` is the engine doing the draw, and `[:count]` bounds it.
        drawn = Question.objects.filter(
            pk__in=[q.pk for q in bank]
        ).order_by("?")[:count]

        data["question_ids"] = [str(q.pk) for q in drawn]
        _save_wizard(request, data)
        messages.success(
            request,
            _("%(count)d questions selected from this subject's bank.") % {"count": count},
        )
        return redirect("teacher:create_competition")

    # -- step advance/retreat ------------------------------------------------

    def _advance(self, request: HttpRequest, data: dict, step: str) -> HttpResponse:
        """Validate the current step and move to the next one."""
        errors: dict[str, str] = {}

        if step == STEP_BASIC:
            basic = data.get("basic", {})
            errors = _validate_basic(basic)
        elif step == STEP_SUBJECT_CLASSROOMS:
            errors = _validate_subject_and_classrooms(data)
        elif step == STEP_SETTINGS:
            errors = _validate_settings(data.get("settings", {}))
        elif step == STEP_QUESTIONS:
            errors = _validate_questions(data.get("question_ids", []))

        if errors:
            context = self._common_context(request, data, step)
            context["errors"] = errors
            context.update(self._step_context(request, data, step))
            return render(request, self.template_name, context)

        # Move to next step
        current_idx = STEP_ORDER.index(step)
        if current_idx < len(STEP_ORDER) - 1:
            data["step"] = STEP_ORDER[current_idx + 1]
            _save_wizard(request, data)
            return redirect("teacher:create_competition")

        # Already at the last step – submit
        return self._create_competition(request, data)

    def _retreat(self, request: HttpRequest, data: dict, step: str) -> HttpResponse:
        """Go back to the previous step."""
        current_idx = STEP_ORDER.index(step)
        if current_idx > 0:
            data["step"] = STEP_ORDER[current_idx - 1]
            _save_wizard(request, data)
        return redirect("teacher:create_competition")

    # -- question selection (POST) -------------------------------------------

    def _add_question(self, request: HttpRequest, data: dict, question_id: str) -> HttpResponse:
        """Add a question to the selection."""
        question_ids = list(data.get("question_ids", []))
        if question_id not in question_ids:
            question_ids.append(question_id)
        data["question_ids"] = question_ids
        _save_wizard(request, data)
        return redirect("teacher:create_competition")

    def _remove_question(self, request: HttpRequest, data: dict, question_id: str) -> HttpResponse:
        """Remove a question from the selection."""
        question_ids = list(data.get("question_ids", []))
        if question_id in question_ids:
            question_ids.remove(question_id)
        data["question_ids"] = question_ids
        _save_wizard(request, data)
        return redirect("teacher:create_competition")

    def _reorder_questions(self, request: HttpRequest, data: dict) -> HttpResponse:
        """Reorder selected questions by a new order string ``q_order``."""
        order = request.POST.get("q_order", "")
        question_ids = data.get("question_ids", [])
        try:
            new_order = [qid for qid in order.split(",") if qid]
            if set(new_order) == set(question_ids) and question_ids:
                data["question_ids"] = new_order
            elif question_ids:
                # Unknown ids: keep selection but preserve known order.
                kept = [qid for qid in question_ids if qid in new_order]
                data["question_ids"] = kept + [
                    qid for qid in question_ids if qid not in new_order
                ]
            _save_wizard(request, data)
        except (ValueError, TypeError):
            pass
        return redirect("teacher:create_competition")

    def _sort_questions_by_id(self, request: HttpRequest, data: dict) -> HttpResponse:
        """Sort the currently selected questions by their database id."""
        question_ids = data.get("question_ids", [])
        try:
            question_ids = sorted(question_ids, key=lambda qid: int(qid))
            data["question_ids"] = question_ids
            _save_wizard(request, data)
        except (ValueError, TypeError):
            pass
        return redirect("teacher:create_competition")

    def _move_question(self, request: HttpRequest, data: dict, direction: str) -> HttpResponse:
        """Move one selected question one place up or down the order of play.

        Plain buttons with a named question and direction, so the order can be
        changed without any JavaScript and survives a reload: the wizard's state
        is the session, and this writes it.
        """
        question_id = (request.POST.get("question_id") or "").strip()
        order = [str(qid) for qid in data.get("question_ids", [])]
        if question_id not in order:
            return redirect("teacher:create_competition")

        index = order.index(question_id)
        neighbour = index - 1 if direction == "up" else index + 1
        if neighbour < 0 or neighbour >= len(order):
            return redirect("teacher:create_competition")

        order[index], order[neighbour] = order[neighbour], order[index]
        data["question_ids"] = order
        _save_wizard(request, data)
        return redirect("teacher:create_competition")

    # -- draft save ---------------------------------------------------------

    def _save_draft(self, request: HttpRequest, data: dict) -> HttpResponse:
        """Persist the current wizard state as a draft Competition."""
        step = data["step"]
        errors: dict[str, str] = {}

        # Validate current step if needed
        if step == STEP_BASIC:
            errors = _validate_basic(data.get("basic", {}))
        elif step == STEP_SUBJECT_CLASSROOMS:
            errors = _validate_subject_and_classrooms(data)
        elif step == STEP_SETTINGS:
            errors = _validate_settings(data.get("settings", {}))
        elif step == STEP_QUESTIONS:
            errors = _validate_questions(data.get("question_ids", []))

        # Per-question overrides are validated on the way in *and* on the way out.
        # The wizard's state lives in the session, so a value that was valid when
        # it was typed can be stale by the time the round is saved, and a
        # zero-point question would quietly change the total the screens and the
        # leaderboard work from.
        errors.update(
            _point_override_errors(
                data.get("question_ids", []), data.get("question_points", {})
            )
        )

        if errors:
            context = self._common_context(request, data, step)
            context["errors"] = errors
            context.update(self._step_context(request, data, step))
            return render(request, self.template_name, context)

        # Create the competition draft using the existing engine
        try:
            with transaction.atomic():
                competition = self._build_competition(request, data)
            _clear_wizard(request)
            messages.success(request, _('Draft "%(title)s" saved.') % {"title": competition.title})
            return self._competition_created_redirect(request, competition)
        except ValidationError as e:
            messages.error(request, str(e))
            return self.get(request)

    # -- competition creation -----------------------------------------------

    def _create_competition(self, request: HttpRequest, data: dict) -> HttpResponse:
        """Create the competition from the wizard data."""
        step = data["step"]
        errors: dict[str, str] = {}

        # Validate all steps
        if step == STEP_BASIC:
            errors = _validate_basic(data.get("basic", {}))
        elif step == STEP_SUBJECT_CLASSROOMS:
            errors = _validate_subject_and_classrooms(data)
        elif step == STEP_SETTINGS:
            errors = _validate_settings(data.get("settings", {}))
        elif step == STEP_QUESTIONS:
            errors = _validate_questions(data.get("question_ids", []))

        # Same re-check as the draft path: the stored overrides are what the round
        # will actually award, so they are validated where they are used rather
        # than trusted because they were typed once.
        errors.update(
            _point_override_errors(
                data.get("question_ids", []), data.get("question_points", {})
            )
        )

        if errors:
            context = self._common_context(request, data, step)
            context["errors"] = errors
            context.update(self._step_context(request, data, step))
            return render(request, self.template_name, context)

        try:
            with transaction.atomic():
                competition = self._build_competition(request, data)
            _clear_wizard(request)
            messages.success(
                request,
                _('Competition "%(title)s" created as draft.')
                % {"title": competition.title},
            )
            return self._competition_created_redirect(request, competition)
        except ValidationError as e:
            messages.error(request, str(e))
            return self.get(request)

    @staticmethod
    def _competition_created_redirect(request: HttpRequest, competition: Competition):
        slot = request.session.pop(COMPETITION_RETURN_KEY, None)
        if slot in {"qualification", "semi_final", "final"}:
            query = urlencode({"competition_id": competition.pk, "stage": slot})
            return redirect(f"{reverse('teacher:create_tournament')}?{query}")
        return redirect("teacher:competition_detail", pk=competition.pk)

    def _build_competition(self, request: HttpRequest, data: dict) -> Competition:
        """Create a Competition from wizard data."""
        from scoring.models import (
            DEFAULT_CORRECT_POINTS,
            DEFAULT_SPEED_BONUS_POINTS,
            ScoringRule,
            SpeedBonusMode,
        )

        basic = data.get("basic", {})
        classroom_ids = list(dict.fromkeys(data.get("classroom_ids", [])))
        settings = data.get("settings", {})
        question_ids = list(dict.fromkeys(str(x) for x in data.get("question_ids", [])))

        # Authorisation is checked before anything is written, not after: the
        # wizard's state lives in the session, so an assignment an administrator
        # removed between the step-2 form and this save must not reach the
        # database at all. Creating the row first and raising afterwards would
        # leave an orphan behind whenever the caller does not wrap this in a
        # transaction. An administrator may run any active classroom, which is
        # what `_authorised_classroom_ids` says.
        allowed_classroom_ids = _authorised_classroom_ids(request.user)
        unauthorised = [
            classroom_id
            for classroom_id in classroom_ids
            if str(classroom_id) not in allowed_classroom_ids
        ]
        if unauthorised:
            logger.warning(
                "Teacher %s tried to save a competition with %d unauthorised classroom(s).",
                request.user.pk,
                len(unauthorised),
            )
            raise ValidationError(
                _(
                    "Some of the chosen classrooms are not assigned to you, so this "
                    "competition was not saved. Choose the classrooms you teach."
                )
            )

        # Lock a reopened draft so a concurrent start cannot be overwritten.
        if data.get("competition_id"):
            competition = get_object_or_404(
                Competition.objects.select_for_update().for_user(request.user),
                pk=data["competition_id"],
            )
            if competition.started_at or competition.state != CompetitionState.WAITING:
                raise ValidationError(_("Only an unstarted draft can be edited."))
            competition.title = basic["title"].strip()
            competition.save(update_fields=["title", "updated_at"])
            competition.classrooms.all().delete()
            competition.questions.all().delete()
        else:
            competition = Competition.objects.create(
                title=basic["title"].strip(), teacher=request.user,
                state=CompetitionState.WAITING,
            )

        # Persist scheduling intent if provided
        planned_for_raw = (settings.get("planned_for") or "").strip()
        if not planned_for_raw and competition.planned_for:
            competition.planned_for = None
            competition.save(update_fields=["planned_for", "updated_at"])
        if planned_for_raw:
            from django.utils import timezone
            from django.utils.dateparse import parse_datetime

            parsed = parse_datetime(planned_for_raw)
            if parsed:
                if timezone.is_naive(parsed):
                    parsed = parsed.replace(tzinfo=timezone.get_current_timezone())
                competition.planned_for = parsed
                competition.save(update_fields=["planned_for", "updated_at"])

        for classroom_id in classroom_ids:
            classroom = get_object_or_404(Classroom, pk=classroom_id)
            CompetitionClassroom.objects.create(
                competition=competition,
                classroom=classroom,
            )

        # Resolve scoring defaults
        points_per_question = int(
            settings.get("points_per_question", DEFAULT_CORRECT_POINTS)
        )
        speed_bonus = int(settings.get("speed_bonus", DEFAULT_SPEED_BONUS_POINTS))
        duration_seconds_setting = int(settings.get("duration_seconds", 30))
        question_points = data.get("question_points", {})

        # Create scoring rule
        ScoringRule.objects.update_or_create(
            competition=competition,
            defaults={
                "correct_points": points_per_question,
                "speed_bonus_max": speed_bonus,
                "speed_bonus_mode": SpeedBonusMode.LINEAR,
            },
        )

        # Add questions in the order the teacher left them in - including a
        # random draw, whose ids were written into the wizard state when it was
        # made. Re-ordering here would make a saved round differ from what the
        # teacher reviewed.
        questions = Question.objects.filter(pk__in=question_ids)
        by_id = {str(q.pk): q for q in questions}
        ordered = [by_id[question_id] for question_id in question_ids if question_id in by_id]
        for position, question in enumerate(ordered, start=1):
            qid_key = str(question.pk)
            override_points = _override_points_for(question_points, qid_key)
            CompetitionQuestion.objects.create(
                competition=competition,
                question=question,
                position=position,
                duration_seconds=duration_seconds_setting,
                points=override_points if override_points is not None else points_per_question,
            )

        # Update total_questions
        competition.total_questions = competition.questions.count()
        competition.save(update_fields=["total_questions", "updated_at"])

        return competition

    # -- context helpers ----------------------------------------------------

    def _common_context(self, request, data, step):
        current_idx = STEP_ORDER.index(step) if step in STEP_ORDER else 0
        wizard_steps = [
            {
                "key": key,
                "label": STEP_LABELS[key],
                "index": idx,
                "state": "completed" if idx < current_idx else ("active" if idx == current_idx else "pending"),
            }
            for idx, key in enumerate(STEP_ORDER)
        ]
        return {
            "wizard": True,
            "step": step,
            "step_labels": STEP_LABELS,
            "step_order": STEP_ORDER,
            "wizard_steps": wizard_steps,
            "is_administrator": is_administrator(request.user),
            "is_teacher": request.user.is_teacher,
            "role_display": request.user.get_role_display(),
            "data": data,
        }

    def _step_context(self, request, data, step):
        """Return context specific to the current step."""
        if step == STEP_BASIC:
            return {
                "title": data.get("basic", {}).get("title", ""),
                "subject": data.get("basic", {}).get("subject", ""),
                "grade": data.get("basic", {}).get("grade", ""),
                "description": data.get("basic", {}).get("description", ""),
            }
        if step == STEP_SUBJECT_CLASSROOMS:
            classrooms = _classrooms_for_user(request.user)
            selected = {
                int(classroom_id)
                for classroom_id in data.get("classroom_ids", [])
                if str(classroom_id).isdigit()
            }
            subjects = Subject.objects.active().order_by("order", "name")
            # Counts per subject, so a teacher choosing a subject is told how
            # many questions there are to pick from before committing to it.
            counts = {
                subject.pk: Question.objects.filter(
                    subject=subject, active=True
                ).count()
                for subject in subjects
            }
            topics = Subject.objects.none()
            subject_id = str(data.get("subject_id") or "").strip()
            if subject_id.isdigit():
                from subjects.models import Topic

                topics = Topic.objects.active().filter(subject_id=subject_id).order_by(
                    "order", "name"
                )
            return {
                "classrooms": classrooms,
                "classroom_targets": screen_targeting.for_ids([c.pk for c in classrooms]).classrooms,
                "selected_classroom_ids": selected,
                "subjects": subjects,
                "subject_counts": counts,
                "selected_subject_id": subject_id,
                "topics": topics,
                "selected_topic_id": str(data.get("topic_id") or "").strip(),
            }
        if step == STEP_SETTINGS:
            from scoring.models import DEFAULT_CORRECT_POINTS, DEFAULT_SPEED_BONUS_POINTS

            settings = data.get("settings", {})
            return {
                "duration_seconds": settings.get("duration_seconds", 30),
                "scoring_mode": settings.get("scoring_mode", "standard"),
                "speed_bonus": settings.get("speed_bonus", DEFAULT_SPEED_BONUS_POINTS),
                "points_per_question": settings.get("points_per_question", DEFAULT_CORRECT_POINTS),
                "planned_for": settings.get("planned_for", ""),
                # A datetime-local control needs "YYYY-MM-DDTHH:MM", and the
                # stored form is "YYYY-MM-DD HH:MM". Convert it here rather than
                # in the template, so the stored value and the two ways of
                # reading it stay in one place.
                "planned_for_input": (settings.get("planned_for", "") or "").replace(
                    " ", "T"
                ),
            }
        if step == STEP_QUESTIONS:
            filters = data.get("filters", {})
            q = filters.get("q", "")
            question_type = filters.get("type", "")
            questions = _questions_for_user(
                request.user,
                q,
                question_type,
                subject_id=data.get("subject_id", ""),
                topic_id=data.get("topic_id", ""),
            )
            selected = [int(qid) for qid in data.get("question_ids", []) if qid]
            # Keyed by integer pk so the template can look a question's override
            # up by `question.pk`; the session stores them keyed by string, which
            # would never match an int key.
            question_points = {
                int(k): v for k, v in data.get("question_points", {}).items()
            }
            return {
                "questions": questions,
                "selected_question_ids": selected,
                "q": q,
                "question_type": question_type,
                "question_type_choices": QuestionType.choices,
                "wizard_subject_name": (
                    Subject.objects.filter(pk=data.get("subject_id")).values_list("name", flat=True).first()
                    if str(data.get("subject_id") or "").isdigit()
                    else ""
                ),
                "create_question_url": (
                    reverse("teacher:create_question") + "?return_to_competition=1"
                ),
                "question_points": question_points,
                "points_per_question": (
                    (data.get("settings", {}) or {}).get("points_per_question", 100)
                ),
            }
        if step == STEP_REVIEW:
            return self._review_context(data)
        return {}

    def _review_context(self, data):
        """Build review page context.

        The review step also carries any per-question override that would not
        survive a save, because the review step is where a teacher reads the
        points and presses Save Draft. Surfacing it only on the questions step
        would mean the very page that shows "0 points" says nothing is wrong with
        it.
        """
        basic = data.get("basic", {})
        classroom_ids = data.get("classroom_ids", [])
        question_ids = data.get("question_ids", [])
        settings = data.get("settings", {})

        classrooms = Classroom.objects.filter(pk__in=classroom_ids).order_by("name")
        questions = Question.objects.filter(pk__in=question_ids).order_by("id")

        from scoring.models import DEFAULT_CORRECT_POINTS, DEFAULT_SPEED_BONUS_POINTS

        points_per_question = int(settings.get("points_per_question", DEFAULT_CORRECT_POINTS))
        question_points = data.get("question_points", {})

        total_points = 0
        # questions is ordered by id; reconstruct in review order (as selected in order)
        by_id = {q.pk: q for q in questions}
        for qid in question_ids:
            try:
                pk = int(qid)
            except (TypeError, ValueError):
                continue
            override = _override_points_for(question_points, qid)
            if override is None:
                total_points += points_per_question
            else:
                total_points += int(override)

        planned_for = (settings.get("planned_for") or "").strip()

        # Per-question rows for the review list, in the order the teacher left
        # them, each carrying the points it will actually be worth (its own
        # override, or the round's default). This is what the review step reads,
        # so what the teacher is shown and what is persisted cannot disagree.
        review_question_rows = []
        for qid in question_ids:
            try:
                question = by_id[int(qid)]
            except (KeyError, TypeError, ValueError):
                continue
            override = _override_points_for(question_points, qid)
            review_question_rows.append(
                {
                    "question": question,
                    "points": int(override) if override is not None else points_per_question,
                    "is_override": override is not None,
                }
            )

        # How long the round will take, and which screens it will reach, resolved
        # from the classrooms the teacher already chose. Shown here, before
        # anything is created, because "which classrooms" is the step that decides
        # them.
        estimated_minutes = (
            len(question_ids) * int(settings.get("duration_seconds", 30))
        ) / 60
        targeting = screen_targeting.for_ids(classroom_ids)

        subject_name = ""
        if str(data.get("subject_id") or "").isdigit():
            subject_name = (
                Subject.objects.filter(pk=data["subject_id"])
                .values_list("name", flat=True)
                .first()
                or ""
            )

        return {
            "review_title": basic.get("title", ""),
            "review_subject_name": subject_name,
            "review_free_subject": basic.get("subject", ""),
            "review_grade": basic.get("grade", ""),
            "review_description": basic.get("description", ""),
            "review_classrooms": classrooms,
            "review_question_count": len(question_ids),
            "review_questions": questions,
            "review_question_rows": review_question_rows,
            "review_duration_seconds": settings.get("duration_seconds", 30),
            "review_scoring_mode": settings.get("scoring_mode", "standard"),
            "review_speed_bonus": settings.get("speed_bonus", DEFAULT_SPEED_BONUS_POINTS),
            "review_points_per_question": points_per_question,
            "review_total_points": total_points,
            "review_estimated_minutes": estimated_minutes,
            "review_targeting": targeting,
            "review_planned_for": planned_for,
            "question_points": question_points,
            # An override that a save would reject, named here rather than only on
            # the questions step: this is the page that shows the points and
            # carries the Save Draft button, so a teacher reading "0 points" must
            # be told here that it will not be accepted.
            "point_errors": _point_override_errors(question_ids, question_points),
        }


class ResumeCompetitionWizardView(CompetitionWizardView):
    """Explicit CSRF-protected reopen; GET never replaces session work."""
    http_method_names = ["post"]

    def post(self, request, pk):
        competition = get_object_or_404(Competition.objects.for_user(request.user), pk=pk)
        if competition.started_at or competition.state != CompetitionState.WAITING:
            raise Http404
        classrooms = list(competition.classrooms.values_list("classroom_id", flat=True))
        if not {str(pk) for pk in classrooms} <= _authorised_classroom_ids(request.user):
            raise Http404
        entries = list(competition.questions.select_related("question").order_by("position"))
        subjects = {e.question.subject_id for e in entries}
        if len(subjects) > 1:
            messages.error(request, _("This draft contains multiple subjects and cannot be reopened in this wizard."))
            return redirect("teacher:competition_detail", pk=pk)
        rule = getattr(competition, "scoring_rule", None)
        from django.utils import timezone
        data = {
            "competition_id": competition.pk, "step": STEP_SUBJECT_CLASSROOMS,
            "basic": {"title": competition.title},
            "classroom_ids": [str(pk) for pk in classrooms],
            "subject_id": str(next(iter(subjects))) if subjects else "", "topic_id": "",
            "question_ids": [str(e.question_id) for e in entries],
            "question_points": {str(e.question_id): e.points for e in entries if e.points is not None},
            "settings": {
                "duration_seconds": entries[0].duration_seconds if entries else 30,
                "points_per_question": rule.correct_points if rule else 100,
                "speed_bonus": rule.speed_bonus_max if rule else 0,
                "scoring_mode": "standard",
                "planned_for": timezone.localtime(competition.planned_for).strftime("%Y-%m-%dT%H:%M") if competition.planned_for else "",
            },
        }
        _save_wizard(request, data)
        return redirect("teacher:create_competition")
