"""Competition wizard for the teacher workspace.

A multi-step guided wizard that lets a teacher create a competition
without seeing Django model internals.

The wizard stores its temporary state in the user's session. On final
submission the existing Competition engine is used so nothing is
duplicated and the live engine is not touched.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.http.request import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from accounts.permissions import is_administrator, is_staff_member
from classrooms.models import Classroom
from competitions.models import Competition, CompetitionClassroom, CompetitionQuestion, CompetitionState
from questions.models import Question, QuestionType


# ---------------------------------------------------------------------------
# Wizard step constants
# ---------------------------------------------------------------------------

STEP_BASIC = "basic"
STEP_CLASSROOMS = "classrooms"
STEP_SETTINGS = "settings"
STEP_QUESTIONS = "questions"
STEP_REVIEW = "review"

STEP_ORDER = [STEP_BASIC, STEP_CLASSROOMS, STEP_SETTINGS, STEP_QUESTIONS, STEP_REVIEW]

STEP_LABELS = {
    STEP_BASIC: "1 Basic Info",
    STEP_CLASSROOMS: "2 Classes",
    STEP_SETTINGS: "3 Settings",
    STEP_QUESTIONS: "4 Questions",
    STEP_REVIEW: "5 Review",
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
    if "settings" not in data:
        data["settings"] = {
            "duration_seconds": 30,
            "scoring_mode": "standard",
            "speed_bonus": 10,
        }
    if "question_ids" not in data:
        data["question_ids"] = []
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


def _questions_for_user(user, q: str = "", question_type: str = "") -> list:
    """Questions a teacher may select from, optionally filtered.

    Filtering is limited to fields that exist on the Question model:
    - text (via ``q`` - matches question text, any type value)
    - question_type (via ``question_type``)

    subject/grade are not part of the Question model and are intentionally
    not exposed as filters.
    """
    qs = Question.objects.filter(active=True).order_by("created_at")
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

def _validate_basic(data: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    title = (data.get("title") or "").strip()
    if not title:
        errors["title"] = "A competition name is required."
    return errors


def _validate_classrooms(classroom_ids: list) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not classroom_ids:
        errors["classrooms"] = "Select at least one classroom."
    return errors


def _validate_settings(settings: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    try:
        duration = int(settings.get("duration_seconds", 0))
        if duration < 5 or duration > 600:
            errors["duration_seconds"] = "Time per question must be between 5 and 600 seconds."
    except (ValueError, TypeError):
        errors["duration_seconds"] = "Enter a valid number of seconds."
    return errors


def _validate_questions(question_ids: list) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not question_ids:
        errors["questions"] = "Select at least one question."
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

        # Question search/filter only changes results, never the wizard state.
        if step == STEP_QUESTIONS:
            q = request.GET.get("q", "")
            question_type = request.GET.get("type", "")
            data["filters"] = {"q": q, "type": question_type}
            _save_wizard(request, data)

        context = self._common_context(request, data, step)
        context.update(self._step_context(request, data, step))
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        data = _wizard_data(request)
        step = data["step"]
        action = request.POST.get("action", step)

        # Persist POST data for the current step before acting
        if step == STEP_BASIC:
            data["basic"] = {
                "title": request.POST.get("title", ""),
                "subject": request.POST.get("subject", ""),
                "grade": request.POST.get("grade", ""),
                "description": request.POST.get("description", ""),
            }
        elif step == STEP_CLASSROOMS:
            data["classroom_ids"] = request.POST.getlist("classroom_ids")
        elif step == STEP_SETTINGS:
            data["settings"] = {
                "duration_seconds": int(request.POST.get("duration_seconds", 30)),
                "scoring_mode": request.POST.get("scoring_mode", "standard"),
                "speed_bonus": int(request.POST.get("speed_bonus", 10)),
            }
        elif step == STEP_QUESTIONS:
            data["question_ids"] = request.POST.getlist("question_ids")
        _save_wizard(request, data)

        # Handle step-specific actions
        if action == "cancel":
            _clear_wizard(request)
            messages.info(request, "Competition creation cancelled.")
            return redirect("teacher:dashboard")

        if action == "save_draft":
            return self._save_draft(request, data)

        if action == "next":
            return self._advance(request, data, step)

        if action == "back":
            return self._retreat(request, data, step)

        if action == "submit":
            return self._create_competition(request, data)

        # Unknown action – stay on current step
        messages.error(request, f"Unknown action: {action!r}.")
        return self.get(request, *args, **kwargs)

    # -- step advance/retreat ------------------------------------------------

    def _advance(self, request: HttpRequest, data: dict, step: str) -> HttpResponse:
        """Validate the current step and move to the next one."""
        errors: dict[str, str] = {}

        if step == STEP_BASIC:
            basic = data.get("basic", {})
            errors = _validate_basic(basic)
        elif step == STEP_CLASSROOMS:
            errors = _validate_classrooms(data.get("classroom_ids", []))
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

    # -- draft save ---------------------------------------------------------

    def _save_draft(self, request: HttpRequest, data: dict) -> HttpResponse:
        """Persist the current wizard state as a draft Competition."""
        step = data["step"]
        errors: dict[str, str] = {}

        # Validate current step if needed
        if step == STEP_BASIC:
            errors = _validate_basic(data.get("basic", {}))
        elif step == STEP_CLASSROOMS:
            errors = _validate_classrooms(data.get("classroom_ids", []))
        elif step == STEP_SETTINGS:
            errors = _validate_settings(data.get("settings", {}))
        elif step == STEP_QUESTIONS:
            errors = _validate_questions(data.get("question_ids", []))

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
            messages.success(request, f'Draft "{competition.title}" saved.')
            return redirect("teacher:competition_detail", pk=competition.pk)
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
        elif step == STEP_CLASSROOMS:
            errors = _validate_classrooms(data.get("classroom_ids", []))
        elif step == STEP_SETTINGS:
            errors = _validate_settings(data.get("settings", {}))
        elif step == STEP_QUESTIONS:
            errors = _validate_questions(data.get("question_ids", []))

        if errors:
            context = self._common_context(request, data, step)
            context["errors"] = errors
            context.update(self._step_context(request, data, step))
            return render(request, self.template_name, context)

        try:
            with transaction.atomic():
                competition = self._build_competition(request, data)
            _clear_wizard(request)
            messages.success(request, f'Competition "{competition.title}" created as draft.')
            return redirect("teacher:competition_detail", pk=competition.pk)
        except ValidationError as e:
            messages.error(request, str(e))
            return self.get(request)

    def _build_competition(self, request: HttpRequest, data: dict) -> Competition:
        """Create a Competition from wizard data."""
        basic = data.get("basic", {})
        classroom_ids = data.get("classroom_ids", [])
        settings = data.get("settings", {})
        question_ids = data.get("question_ids", [])

        # Create the competition
        competition = Competition.objects.create(
            title=basic["title"].strip(),
            teacher=request.user,
            state=CompetitionState.WAITING,
        )

        # Add classrooms
        for classroom_id in classroom_ids:
            classroom = get_object_or_404(Classroom, pk=classroom_id)
            CompetitionClassroom.objects.create(
                competition=competition,
                classroom=classroom,
            )

        # Add questions in order
        questions = Question.objects.filter(pk__in=question_ids).order_by("id")
        for position, question in enumerate(questions, start=1):
            CompetitionQuestion.objects.create(
                competition=competition,
                question=question,
                position=position,
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
        if step == STEP_CLASSROOMS:
            classrooms = _classrooms_for_user(request.user)
            selected = set(data.get("classroom_ids", []))
            return {
                "classrooms": classrooms,
                "selected_classroom_ids": selected,
            }
        if step == STEP_SETTINGS:
            settings = data.get("settings", {})
            return {
                "duration_seconds": settings.get("duration_seconds", 30),
                "scoring_mode": settings.get("scoring_mode", "standard"),
                "speed_bonus": settings.get("speed_bonus", 10),
            }
        if step == STEP_QUESTIONS:
            filters = data.get("filters", {})
            q = filters.get("q", "")
            question_type = filters.get("type", "")
            questions = _questions_for_user(request.user, q, question_type)
            selected = [int(qid) for qid in data.get("question_ids", []) if qid]
            return {
                "questions": questions,
                "selected_question_ids": selected,
                "q": q,
                "question_type": question_type,
                "question_type_choices": QuestionType.choices,
            }
        if step == STEP_REVIEW:
            return self._review_context(data)
        return {}

    def _review_context(self, data):
        """Build review page context."""
        basic = data.get("basic", {})
        classroom_ids = data.get("classroom_ids", [])
        question_ids = data.get("question_ids", [])
        settings = data.get("settings", {})

        classrooms = Classroom.objects.filter(pk__in=classroom_ids).order_by("name")
        questions = Question.objects.filter(pk__in=question_ids).order_by("id")

        estimated_minutes = (len(question_ids) * int(settings.get("duration_seconds", 30))) / 60

        return {
            "review_title": basic.get("title", ""),
            "review_subject": basic.get("subject", ""),
            "review_grade": basic.get("grade", ""),
            "review_description": basic.get("description", ""),
            "review_classrooms": classrooms,
            "review_question_count": len(question_ids),
            "review_questions": questions,
            "review_duration_seconds": settings.get("duration_seconds", 30),
            "review_scoring_mode": settings.get("scoring_mode", "standard"),
            "review_speed_bonus": settings.get("speed_bonus", 10),
            "review_estimated_minutes": estimated_minutes,
        }
