"""Guided tournament setup for the teacher workspace.

The wizard keeps uncommitted choices in the session and builds the tournament
through the existing tournament services only after the review step is confirmed.
Competitions continue to be created by :class:`teacher.wizard.CompetitionWizardView`.
"""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from accounts.permissions import is_administrator, is_staff_member
from classrooms.models import Classroom
from competitions.models import Competition, CompetitionState
from tournaments import services
from tournaments.models import TournamentStageType

SESSION_KEY = "teacher_tournament_wizard"
COMPETITION_RETURN_KEY = "teacher_tournament_competition_return"

STEP_TOURNAMENT = "tournament"
STEP_PARTICIPANTS = "participants"
STEP_STAGES = "stages"
STEP_ADVANCEMENT = "advancement"
STEP_COMPETITIONS = "competitions"
STEP_REVIEW = "review"
STEP_CREATE = "create"

STEP_ORDER = (
    STEP_TOURNAMENT,
    STEP_PARTICIPANTS,
    STEP_STAGES,
    STEP_ADVANCEMENT,
    STEP_COMPETITIONS,
    STEP_REVIEW,
    STEP_CREATE,
)
STEP_LABELS = {
    STEP_TOURNAMENT: _("Tournament"),
    STEP_PARTICIPANTS: _("Participants"),
    STEP_STAGES: _("Stages"),
    STEP_ADVANCEMENT: _("Advancement"),
    STEP_COMPETITIONS: _("Competitions"),
    STEP_REVIEW: _("Review"),
    STEP_CREATE: _("Create"),
}

STAGE_TEMPLATES = {
    "qualification_final": [
        (TournamentStageType.QUALIFICATION, _("Qualification")),
        (TournamentStageType.FINAL, _("Final")),
    ],
    "qualification_semi_final": [
        (TournamentStageType.QUALIFICATION, _("Qualification")),
        (TournamentStageType.SEMI_FINAL, _("Semi Final")),
        (TournamentStageType.FINAL, _("Final")),
    ],
}


def _empty_data() -> dict:
    return {
        "step": STEP_TOURNAMENT,
        "tournament": {
            "name": "",
            "subject": "",
            "grade": "",
            "season": str(datetime.date.today().year),
            "description": "",
        },
        "classroom_ids": [],
        "stage_template": "qualification_final",
        "advancing_count": "2",
        "semi_advancing_count": "2",
        "competition_ids": {"qualification": [], "semi_final": [], "final": []},
    }


def _wizard_data(request: HttpRequest) -> dict:
    stored = request.session.get(SESSION_KEY)
    if not isinstance(stored, dict):
        stored = {}
    data = _empty_data()
    tournament = stored.get("tournament")
    if isinstance(tournament, dict):
        data["tournament"].update(tournament)
    if stored.get("step") in STEP_ORDER:
        data["step"] = stored["step"]
    if isinstance(stored.get("classroom_ids"), list):
        data["classroom_ids"] = [str(pk) for pk in stored["classroom_ids"]]
    if stored.get("stage_template") in STAGE_TEMPLATES:
        data["stage_template"] = stored["stage_template"]
    for key in ("advancing_count", "semi_advancing_count"):
        if key in stored:
            data[key] = str(stored[key])
    competition_ids = stored.get("competition_ids")
    if isinstance(competition_ids, dict):
        for slot in data["competition_ids"]:
            ids = competition_ids.get(slot, [])
            if isinstance(ids, list):
                data["competition_ids"][slot] = list(
                    dict.fromkeys(str(pk) for pk in ids if str(pk).isdigit())
                )
    return data


def _save(request: HttpRequest, data: dict) -> None:
    request.session[SESSION_KEY] = data


def _eligible_classrooms(user):
    queryset = Classroom.objects.filter(active=True).annotate(
        tournament_participation_count=Count("tournament_entries")
    )
    if not is_administrator(user):
        queryset = queryset.filter(teachers=user)
    return queryset.order_by("name")


def _selected_classroom_ids(data: dict, user) -> list[int]:
    eligible = set(_eligible_classrooms(user).values_list("pk", flat=True))
    result = []
    for raw_pk in data.get("classroom_ids", []):
        try:
            pk = int(raw_pk)
        except (TypeError, ValueError):
            continue
        if pk in eligible and pk not in result:
            result.append(pk)
    return result


def _available_competitions(user, data: dict) -> dict[str, list[Competition]]:
    classroom_ids = set(_selected_classroom_ids(data, user))
    if not classroom_ids:
        return {slot: [] for slot in ("qualification", "semi_final", "final")}

    queryset = (
        Competition.objects.filter(
            teacher=user,
            state=CompetitionState.WAITING,
            started_at__isnull=True,
            tournament_stage__isnull=True,
        )
        .prefetch_related("classrooms__classroom")
        .order_by("-created_at")
    )
    eligible = [
        competition
        for competition in queryset
        if competition.participating_classroom_ids
        and competition.participating_classroom_ids.issubset(classroom_ids)
    ]
    return {
        slot: eligible
        for slot in ("qualification", "semi_final", "final")
    }


class TournamentWizardView(View):
    """Seven-step teacher flow for planning and creating a tournament."""

    template_name = "teacher/tournament_form.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_staff_member(request.user):
            raise Http404("This account has no assigned role.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        data = _wizard_data(request)
        self._receive_created_competition(request, data)

        create_slot = request.GET.get("create_competition")
        if create_slot:
            if data["step"] != STEP_COMPETITIONS or create_slot not in self._slots(data):
                raise Http404("This competition setup request is no longer available.")
            _save(request, data)
            self._start_competition_wizard(request, data, create_slot)
            return redirect("teacher:create_competition")

        _save(request, data)
        context = self._context(request, data)
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        data = _wizard_data(request)
        step = data["step"]
        action = request.POST.get("action", "next")
        errors: dict[str, str] = {}

        if step == STEP_TOURNAMENT:
            data["tournament"] = {
                "name": request.POST.get("name", "").strip(),
                "subject": request.POST.get("subject", "").strip(),
                "grade": request.POST.get("grade", "").strip(),
                "season": request.POST.get("season", "").strip(),
                "description": request.POST.get("description", "").strip(),
            }
            if action == "next" and not data["tournament"]["name"]:
                errors["name"] = _("Enter a tournament name.")
        elif step == STEP_PARTICIPANTS:
            eligible_ids = set(
                _eligible_classrooms(request.user).values_list("pk", flat=True)
            )
            raw_ids = request.POST.getlist("classroom_ids")
            data["classroom_ids"] = list(
                dict.fromkeys(
                    raw for raw in raw_ids
                    if raw.isdigit() and int(raw) in eligible_ids
                )
            )
            if action == "next" and any(
                not raw.isdigit() or int(raw) not in eligible_ids
                for raw in raw_ids
            ):
                errors["classrooms"] = (
                    _("One or more selected classrooms are no longer available.")
                )
            if action == "next" and len(data["classroom_ids"]) < 2 and not errors:
                errors["classrooms"] = _("Select at least two eligible classrooms.")
        elif step == STEP_STAGES:
            data["stage_template"] = request.POST.get("stage_template", "")
            if action == "next" and data["stage_template"] not in STAGE_TEMPLATES:
                errors["stage_template"] = _("Choose one of the stage templates.")
        elif step == STEP_ADVANCEMENT:
            data["advancing_count"] = request.POST.get("advancing_count", "").strip()
            data["semi_advancing_count"] = request.POST.get(
                "semi_advancing_count", ""
            ).strip()
            if action == "next":
                errors = self._validate_advancement(data, len(data["classroom_ids"]))
        elif step == STEP_COMPETITIONS:
            available = _available_competitions(request.user, data)
            selected = {}
            invalid_slots = []
            for slot in self._slots(data):
                allowed = {str(item.pk) for item in available[slot]}
                posted = request.POST.getlist(f"competition_{slot}")
                if any(value not in allowed for value in posted):
                    invalid_slots.append(slot)
                selected[slot] = list(
                    dict.fromkeys(
                        value
                        for value in posted
                        if value in allowed
                    )
                )
            data["competition_ids"] = {
                **data["competition_ids"],
                **selected,
            }
            if action == "next":
                errors = self._validate_selected_competitions(
                    request.user, data, available
                )
                for slot in invalid_slots:
                    errors[f"competition_{slot}"] = (
                        _("One or more selected competitions are no longer available.")
                    )

        if action == "cancel":
            self._clear(request)
            messages.info(request, _("Tournament setup cancelled."))
            return redirect("teacher:tournaments")

        if action == "back":
            index = STEP_ORDER.index(step)
            if index:
                data["step"] = STEP_ORDER[index - 1]
            _save(request, data)
            return redirect("teacher:create_tournament")

        if action == "create":
            if step != STEP_CREATE:
                raise Http404("The tournament is not ready to create.")
            errors = self._validate_all(request.user, data)
            if not errors:
                return self._create(request, data)

        if action == "next" and not errors:
            index = STEP_ORDER.index(step)
            if index < len(STEP_ORDER) - 1:
                data["step"] = STEP_ORDER[index + 1]
            _save(request, data)
            return redirect("teacher:create_tournament")

        _save(request, data)
        context = self._context(request, data)
        context["errors"] = errors
        context["competition_errors"] = {
            slot: errors.get(f"competition_{slot}", "")
            for slot in self._slots(data)
        }
        return render(request, self.template_name, context, status=400 if errors else 200)

    @staticmethod
    def _slots(data: dict) -> list[str]:
        if data["stage_template"] == "qualification_semi_final":
            return ["qualification", "semi_final", "final"]
        return ["qualification", "final"]

    @staticmethod
    def _validate_advancement(data: dict, participant_count: int) -> dict[str, str]:
        errors = {}
        try:
            advancing = int(data["advancing_count"])
            if not 1 <= advancing < participant_count:
                errors["advancing_count"] = (
                    _("Choose between 1 and %(count)s classrooms.")
                    % {"count": participant_count - 1}
                )
        except (TypeError, ValueError):
            advancing = 0
            errors["advancing_count"] = _("Enter a valid number of classrooms.")

        if data["stage_template"] == "qualification_semi_final":
            try:
                semi_advancing = int(data["semi_advancing_count"])
                if not 1 <= semi_advancing <= advancing:
                    errors["semi_advancing_count"] = (
                        _("Choose between 1 and %(count)s classrooms.")
                        % {"count": advancing}
                    )
            except (TypeError, ValueError):
                errors["semi_advancing_count"] = _("Enter a valid number of classrooms.")
        return errors

    @staticmethod
    def _validate_selected_competitions(user, data, available) -> dict[str, str]:
        errors = {}
        for slot in TournamentWizardView._slots(data):
            valid = {str(competition.pk) for competition in available[slot]}
            selected = data["competition_ids"].get(slot, [])
            if any(pk not in valid for pk in selected):
                errors[f"competition_{slot}"] = (
                    _("One or more selected competitions are no longer available.")
                )
        return errors

    def _validate_all(self, user, data: dict) -> dict[str, str]:
        errors = {}
        if not data["tournament"]["name"]:
            errors["name"] = _("Enter a tournament name.")
        classroom_ids = _selected_classroom_ids(data, user)
        if len(classroom_ids) < 2:
            errors["classrooms"] = _("Select at least two eligible classrooms.")
        errors.update(self._validate_advancement(data, len(classroom_ids)))
        available = _available_competitions(user, data)
        errors.update(self._validate_selected_competitions(user, data, available))
        return errors

    def _context(self, request: HttpRequest, data: dict) -> dict:
        index = STEP_ORDER.index(data["step"])
        stages = [
            {"key": key, "label": STEP_LABELS[key], "state":
                "complete" if position < index else
                "active" if position == index else "pending"}
            for position, key in enumerate(STEP_ORDER)
        ]
        classrooms = list(_eligible_classrooms(request.user))
        selected_ids = set(_selected_classroom_ids(data, request.user))
        selected_classrooms = [c for c in classrooms if c.pk in selected_ids]
        slots = self._slots(data)
        # Translated here rather than in the template, so the wizard's own step
        # labels, the review summary and the "Create X competition" links all read
        # from one place. `gettext_lazy` means the label is resolved per request:
        # the same view renders these in English or Arabic depending on the
        # active language.
        labels = {
            "qualification": _("Qualification"),
            "semi_final": _("Semi Final"),
            "final": _("Final"),
        }
        competitions = _available_competitions(request.user, data)
        selected_competitions = {
            slot: [
                competition
                for competition in competitions[slot]
                if str(competition.pk) in data["competition_ids"].get(slot, [])
            ]
            for slot in slots
        }
        review_stages = []
        for slot in slots:
            if slot == "qualification":
                advancing = int(data["advancing_count"]) if str(data["advancing_count"]).isdigit() else 0
            elif slot == "semi_final":
                advancing = int(data["semi_advancing_count"]) if str(data["semi_advancing_count"]).isdigit() else 0
            else:
                advancing = None
            review_stages.append({
                "key": slot,
                "name": labels[slot],
                "advancing_count": advancing,
                "competitions": selected_competitions[slot],
            })
        return {
            "wizard_steps": stages,
            "step": data["step"],
            "data": data,
            "classrooms": classrooms,
            "selected_classroom_ids": selected_ids,
            "selected_classrooms": selected_classrooms,
            "stage_templates": [
                {
                    "key": "qualification_final",
                    "label": _("Qualification → Final"),
                    "stages": [_("Qualification"), _("Final")],
                },
                {
                    "key": "qualification_semi_final",
                    "label": _("Qualification → Semi Final → Final"),
                    "stages": [_("Qualification"), _("Semi Final"), _("Final")],
                },
            ],
            "stage_slots": slots,
            "stage_labels": labels,
            "available_competitions": competitions,
            "selected_competitions": selected_competitions,
            "review_stages": review_stages,
            "competition_errors": {
                slot: ""
                for slot in slots
            },
            "is_administrator": is_administrator(request.user),
            "is_teacher": request.user.is_teacher,
            "role_display": request.user.get_role_display(),
            "errors": {},
        }

    def _receive_created_competition(self, request, data: dict) -> None:
        raw_pk = request.GET.get("competition_id")
        slot = request.GET.get("stage")
        if not raw_pk or not slot:
            return
        if data["step"] != STEP_COMPETITIONS or slot not in self._slots(data):
            messages.error(request, _("That competition cannot be added to this setup."))
            return
        try:
            competition = Competition.objects.get(
                pk=int(raw_pk),
                teacher=request.user,
                state=CompetitionState.WAITING,
                started_at__isnull=True,
                tournament_stage__isnull=True,
            )
        except (Competition.DoesNotExist, TypeError, ValueError):
            messages.error(request, _("The created competition is no longer available."))
            return
        classroom_ids = set(_selected_classroom_ids(data, request.user))
        if not competition.participating_classroom_ids.issubset(classroom_ids):
            messages.error(
                request,
                _("Every classroom in the competition must be a tournament participant."),
            )
            return
        selected = data["competition_ids"].setdefault(slot, [])
        if str(competition.pk) not in selected:
            selected.append(str(competition.pk))
            _save(request, data)
            messages.success(
                request,
                _('Added "%(title)s" to %(stage)s.')
                % {"title": competition.title, "stage": _(slot.replace("_", " ").title())},
            )

    def _start_competition_wizard(
        self, request: HttpRequest, data: dict, slot: str
    ) -> None:
        from teacher.wizard import (
            SESSION_KEY as COMPETITION_SESSION_KEY,
            STEP_BASIC,
        )

        # Deliberately NOT translated: this string seeds a competition title that
        # is stored in the database. Pre-filling it through gettext would make the
        # persisted value depend on the teacher's interface language, so the same
        # handoff toggled between Arabic and English would write two different
        # titles - and teacher-created content must be the teacher's, not the
        # active locale's. The stage label shown in the interface is translated
        # separately in `_context`.
        stage_label = {
            "qualification": "Qualification",
            "semi_final": "Semi Final",
            "final": "Final",
        }[slot]
        basic = data["tournament"]
        request.session[COMPETITION_SESSION_KEY] = {
            "step": STEP_BASIC,
            "basic": {
                "title": f"{basic['name']} — {stage_label}",
                "subject": basic["subject"],
                "grade": basic["grade"],
                "description": "",
            },
            "classroom_ids": list(data["classroom_ids"]),
            "settings": {
                "duration_seconds": 30,
                "scoring_mode": "standard",
                "speed_bonus": 10,
            },
            "question_ids": [],
        }
        request.session[COMPETITION_RETURN_KEY] = slot

    def _create(self, request: HttpRequest, data: dict) -> HttpResponse:
        try:
            with transaction.atomic():
                fields = data["tournament"]
                tournament = services.create_tournament(
                    created_by=request.user,
                    name=fields["name"],
                    subject=fields["subject"],
                    grade=fields["grade"],
                    season=fields["season"],
                    description=fields["description"],
                )
                classrooms = {
                    classroom.pk: classroom
                    for classroom in _eligible_classrooms(request.user).filter(
                        pk__in=_selected_classroom_ids(data, request.user)
                    )
                }
                for classroom in classrooms.values():
                    services.add_participant(tournament, classroom)

                stages = {}
                advancing_count = int(data["advancing_count"])
                semi_advancing_count = (
                    int(data["semi_advancing_count"])
                    if data["stage_template"] == "qualification_semi_final"
                    else None
                )
                # The second member of each STAGE_TEMPLATES entry is the stage's
                # translated label. It is deliberately unpacked as `stage_label`
                # and not `_`: `_` is gettext_lazy, and shadowing it here breaks
                # the gettext calls further down in this same method.
                for position, (stage_type, stage_label) in enumerate(
                    STAGE_TEMPLATES[data["stage_template"]], start=1
                ):
                    if stage_type == TournamentStageType.QUALIFICATION:
                        advancing = advancing_count
                    elif stage_type == TournamentStageType.SEMI_FINAL:
                        advancing = semi_advancing_count
                    else:
                        advancing = None
                    stages[stage_type] = services.create_stage(
                        tournament,
                        stage_type,
                        advancing_count=advancing,
                        order=position,
                    )

                competitions = _available_competitions(request.user, data)
                slot_to_stage = {
                    "qualification": TournamentStageType.QUALIFICATION,
                    "semi_final": TournamentStageType.SEMI_FINAL,
                    "final": TournamentStageType.FINAL,
                }
                for slot in self._slots(data):
                    choices = {
                        str(comp.pk): comp for comp in competitions[slot]
                    }
                    for competition_id in data["competition_ids"].get(slot, []):
                        competition = choices.get(str(competition_id))
                        if competition is None:
                            raise services.TournamentError(
                                "competition_unavailable",
                                _("A selected competition is no longer available."),
                            )
                        services.attach_competition(
                            stages[slot_to_stage[slot]], competition
                        )
        except services.TournamentError as error:
            messages.error(request, error.message)
            context = self._context(request, data)
            context["errors"] = {"create": error.message}
            return render(request, self.template_name, context, status=400)

        self._clear(request)
        messages.success(
            request,
            _('Tournament "%(name)s" created. Open it when its setup is ready.')
            % {"name": tournament.name},
        )
        return redirect("teacher:tournament_detail", pk=tournament.pk)

    @staticmethod
    def _clear(request: HttpRequest) -> None:
        request.session.pop(SESSION_KEY, None)
        request.session.pop(COMPETITION_RETURN_KEY, None)

    def get_login_url(self) -> str:
        return reverse("accounts:login")
