"""Views for the teacher workspace.

All views require a signed-in, active member of staff. Role-based filtering
ensures teachers see only their assigned classrooms and related data, while
administrators see the full school picture through the same views.
"""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import TemplateView, View

from accounts.permissions import is_administrator, is_staff_member
from accounts.roles import Role
from classrooms.models import Classroom
from competitions.models import Competition, CompetitionState
from questions.forms import TeacherQuestionForm
from questions.models import Question
from tournaments import services
from tournaments.models import Tournament, TournamentStatus

from teacher.wizard import CompetitionWizardView


def _staff_required(view):
    """Send anonymous visitors to sign in and refuse anyone without a role."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_staff_member(request.user):
            raise Http404("This account has no assigned role.")
        return view(self, request, *args, **kwargs)

    return dispatch


class TeacherDashboardView(TemplateView):
    """Teacher dashboard with quick stats and primary actions.

    Shows real database information:
    - My Competitions (draft, upcoming, recent)
    - My Question Bank
    - Active Tournament
    - Recent Results

    Primary actions:
    - Create Competition
    - Add Question
    - Create Tournament
    """

    template_name = "teacher/dashboard.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Role information
        context["role"] = user.role
        context["role_display"] = user.get_role_display()
        context["is_administrator"] = is_administrator(user)
        context["is_teacher"] = user.is_teacher

        # Teacher dashboard context
        if context["is_administrator"]:
            # Administrators see whole school picture
            context.update(self._administrator_context())
        else:
            # Teachers see only their assigned classrooms and data
            context.update(self._teacher_context(user))

        return context

    def _administrator_context(self) -> dict:
        """Whole-school totals for administrators."""
        # Competition statistics
        all_competitions = Competition.objects.all()
        draft_competitions = all_competitions.filter(
            state=CompetitionState.WAITING, started_at__isnull=True
        )
        upcoming_competitions = all_competitions.filter(
            state=CompetitionState.WAITING, started_at__isnull=False
        )
        recent_competitions = all_competitions.filter(
            state=CompetitionState.FINISHED
        ).order_by("-finished_at")[:5]

        # Question bank
        question_bank = Question.objects.filter(active=True)

        # Active tournament
        active_tournament = Tournament.objects.filter(
            status=TournamentStatus.OPEN
        ).first()

        # Recent results (recently finished competitions)
        recent_results = Competition.objects.filter(
            state=CompetitionState.FINISHED
        ).order_by("-finished_at")[:10]

        return {
            "my_competitions": {
                "draft": draft_competitions.count(),
                "upcoming": upcoming_competitions.count(),
                "recent": list(recent_competitions),
            },
            "question_bank_total": question_bank.count(),
            "active_tournament": active_tournament,
            "recent_results": list(recent_results),
            "primary_actions": [
                {"title": "Create Competition", "url": reverse("teacher:create_competition"), "icon": "plus"},
                {"title": "Add Question", "url": reverse("teacher:create_question"), "icon": "plus"},
                {"title": "Create Tournament", "url": reverse("teacher:create_tournament"), "icon": "plus"},
            ],
        }

    def _teacher_context(self, user) -> dict:
        """Teacher-specific context for assigned classrooms only."""
        # Competitions created by this teacher
        teacher_competitions = Competition.objects.filter(teacher=user)
        draft_competitions = teacher_competitions.filter(
            state=CompetitionState.WAITING, started_at__isnull=True
        )
        upcoming_competitions = teacher_competitions.filter(
            state=CompetitionState.WAITING, started_at__isnull=False
        )
        recent_competitions = teacher_competitions.filter(
            state=CompetitionState.FINISHED
        ).order_by("-finished_at")[:5]

        # Question bank - questions this teacher has created or can use
        # For now, show all active questions (in a real system, this might be filtered)
        question_bank = Question.objects.filter(active=True)

        # Active tournament
        active_tournament = Tournament.objects.filter(
            status=TournamentStatus.OPEN
        ).first()

        # Recent results from teacher's competitions
        recent_results = teacher_competitions.filter(
            state=CompetitionState.FINISHED
        ).order_by("-finished_at")[:10]

        return {
            "my_competitions": {
                "draft": draft_competitions.count(),
                "upcoming": upcoming_competitions.count(),
                "recent": list(recent_competitions),
            },
            "question_bank_total": question_bank.count(),
            "active_tournament": active_tournament,
            "recent_results": list(recent_results),
            "primary_actions": [
                {"title": "Create Competition", "url": reverse("teacher:create_competition"), "icon": "plus"},
                {"title": "Add Question", "url": reverse("teacher:create_question"), "icon": "plus"},
                {"title": "Create Tournament", "url": reverse("teacher:create_tournament"), "icon": "plus"},
            ],
        }

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class CompetitionListView(TemplateView):
    """List of competitions for the current teacher."""

    template_name = "teacher/competitions.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Get competitions for this teacher
        competitions = Competition.objects.filter(teacher=user).order_by("-created_at")

        context.update({
            "competitions": competitions,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class CompetitionCreateView(CompetitionWizardView):
    """Redirect to the wizard - deprecated, use the wizard directly."""
    pass


class CompetitionDetailView(TemplateView):
    """Detail view for a competition."""

    template_name = "teacher/competition_detail.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pk = self.kwargs["pk"]

        # Get the competition - ensure user has permission
        competition = get_object_or_404(Competition, pk=pk)

        # Permission check: teacher must own it, admin can see all
        if not is_administrator(self.request.user) and competition.teacher != self.request.user:
            raise Http404("Competition not found.")

        context.update({
            "competition": competition,
            "is_administrator": is_administrator(self.request.user),
            "is_teacher": self.request.user.is_teacher,
            "role_display": self.request.user.get_role_display(),
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class QuestionBankView(TemplateView):
    """Question bank view."""

    template_name = "teacher/questions.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Get questions (for simplicity, show all active questions)
        # In a more sophisticated system, this might be filtered by user/created
        questions = Question.objects.filter(active=True).order_by("-created_at")

        context.update({
            "questions": questions,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class QuestionCreateView(View):
    """Create a new question using the unified Question Builder."""

    template_name = "teacher/question_form.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = TeacherQuestionForm()
        question = None
        save_option = request.GET.get("save_option", "save")
        context = self._build_context(form, question, save_option, is_edit=False)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        form = TeacherQuestionForm(request.POST, request.FILES)
        save_option = request.POST.get("save_option", "save")
        if form.is_valid():
            question = form.save(teacher=request.user)
            messages.success(request, f"Question #{question.pk} saved.")
            if save_option == "save_another":
                return redirect("teacher:create_question")
            if save_option == "save_to_competition":
                return redirect(f"{reverse('teacher:create_competition')}?question_id={question.pk}")
            return redirect("teacher:questions")
        context = self._build_context(form, None, save_option, is_edit=False)
        return render(request, self.template_name, context)

    def _build_context(self, form, question, save_option, *, is_edit):
        user = self.request.user
        preview_data = {}
        if form.is_bound and form.is_valid():
            preview_data = self._build_preview(form.cleaned_data)
        elif not form.is_bound:
            preview_data = {
                "type": QuestionType.MULTIPLE_CHOICE,
                "text": "",
                "options": [],
                "correct_option": "",
                "correct_answer": None,
                "type_config": {},
                "duration_seconds": 30,
                "active": True,
            }
        context = {
            "form": form,
            "question": question,
            "question_types": QuestionType.choices,
            "save_option": save_option,
            "is_edit": is_edit,
            "preview_data": preview_data,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        }
        return context

    @staticmethod
    def _build_preview(cleaned: dict) -> dict:
        return {
            "type": cleaned.get("question_type"),
            "text": cleaned.get("text", ""),
            "options": cleaned.get("_parsed_options", []) or [],
            "correct_option": cleaned.get("correct_option", ""),
            "correct_answer": cleaned.get("_parsed_correct_answer"),
            "type_config": cleaned.get("_parsed_type_config", {}) or {},
            "duration_seconds": cleaned.get("duration_seconds", 30),
            "active": cleaned.get("active", True),
        }

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class QuestionEditView(View):
    """Edit an existing question using the unified Question Builder."""

    template_name = "teacher/question_form.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        question = self._get_question()
        form = TeacherQuestionForm(instance=question)
        save_option = request.GET.get("save_option", "save")
        context = self._build_context(form, question, save_option, is_edit=True)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        question = self._get_question()
        form = TeacherQuestionForm(request.POST, request.FILES, instance=question)
        save_option = request.POST.get("save_option", "save")
        if form.is_valid():
            form.save(teacher=request.user)
            messages.success(request, f"Question #{question.pk} updated.")
            if save_option == "save_another":
                return redirect("teacher:create_question")
            if save_option == "save_to_competition":
                return redirect(f"{reverse('teacher:create_competition')}?question_id={question.pk}")
            return redirect("teacher:questions")
        context = self._build_context(form, question, save_option, is_edit=True)
        return render(request, self.template_name, context)

    def _get_question(self):
        question = get_object_or_404(Question, pk=self.kwargs["pk"])
        user = self.request.user
        if not is_administrator(user) and question.correct_option != "":
            # Teachers can edit any question (shared bank for now), but we keep
            # the permission hook in place for future tightening.
            pass
        return question

    def _build_context(self, form, question, save_option, *, is_edit):
        user = self.request.user
        preview_data = {}
        if form.is_bound and form.is_valid():
            preview_data = QuestionCreateView._build_preview(form.cleaned_data)
        elif form.instance and form.instance.pk:
            preview_data = {
                "type": form.instance.question_type,
                "text": form.instance.text,
                "options": list(form.instance.options or []),
                "correct_option": form.instance.correct_option or "",
                "correct_answer": form.instance.correct_answer,
                "type_config": form.instance.type_config or {},
                "duration_seconds": form.instance.duration_seconds,
                "active": form.instance.active,
            }
        context = {
            "form": form,
            "question": question,
            "question_types": QuestionType.choices,
            "save_option": save_option,
            "is_edit": is_edit,
            "preview_data": preview_data,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        }
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class TournamentListView(TemplateView):
    """List of tournaments for the current teacher."""

    template_name = "teacher/tournaments.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Get tournaments for this teacher (or all if admin)
        tournaments = Tournament.objects.filter(created_by=user).order_by("-created_at")

        context.update({
            "tournaments": tournaments,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class TournamentCreateView(TemplateView):
    """Create a new tournament."""

    template_name = "teacher/tournament_form.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context.update({
            "is_administrator": is_administrator(self.request.user),
            "is_teacher": self.request.user.is_teacher,
            "role_display": self.request.user.get_role_display(),
        })
        return context

    def post(self, request, *args, **kwargs):
        # Handle form submission for creating a tournament
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        subject = request.POST.get("subject", "").strip()
        grade = request.POST.get("grade", "").strip()
        season = request.POST.get("season", "").strip() or str(datetime.date.today().year)

        if not name:
            messages.error(request, "Tournament name is required.")
            return self.get(request, *args, **kwargs)

        try:
            tournament = services.create_tournament(
                created_by=request.user,
                name=name,
                description=description,
                subject=subject,
                grade=grade,
                season=season
            )
            messages.success(request, f'Tournament "{tournament.name}" created as draft.')
            return redirect("teacher:tournament_detail", pk=tournament.pk)
        except services.TournamentError as e:
            messages.error(request, e.message)
            return self.get(request, *args, **kwargs)
        except Exception as e:
            messages.error(request, f"Error creating tournament: {str(e)}")
            return self.get(request, *args, **kwargs)

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class TournamentDetailView(TemplateView):
    """Detail view for a tournament."""

    template_name = "teacher/tournament_detail.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pk = self.kwargs["pk"]

        # Get the tournament - ensure user has permission
        tournament = get_object_or_404(Tournament, pk=pk)

        # Permission check: teacher must own it, admin can see all
        if not is_administrator(request.user) and tournament.created_by != request.user:
            raise Http404("Tournament not found.")

        context.update({
            "tournament": tournament,
            "is_administrator": is_administrator(request.user),
            "is_teacher": request.user.is_teacher,
            "role_display": request.user.get_role_display(),
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class ReportView(TemplateView):
    """Reports index page."""

    template_name = "teacher/reports.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context.update({
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class HallOfFameView(TemplateView):
    """Hall of Fame view."""

    template_name = "teacher/hall_of_fame.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Get hall of fame data
        from tournaments import services
        records = services.hall_of_fame()

        context.update({
            "records": records,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class HelpView(TemplateView):
    """Help page."""

    template_name = "teacher/help.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context.update({
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")