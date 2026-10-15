"""Views for the teacher workspace.

All views require a signed-in, active member of staff. Role-based filtering
ensures teachers see only their assigned classrooms and related data, while
administrators see the full school picture through the same views.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView, View
from urllib.parse import urlencode

from accounts.permissions import is_administrator, is_staff_member
from competitions.models import Competition, CompetitionState
from live.services import CompetitionControlError, start_competition
from questions.forms import TeacherQuestionForm
from questions.models import Question, QuestionType
from screens import targeting as screen_targeting
from subjects.models import Subject, Topic
from tournaments import services
from tournaments.models import Tournament, TournamentStatus


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

        # Get competitions visible to this user
        competitions = Competition.objects.for_user(user).order_by("-created_at")

        status = (self.request.GET.get("status") or "").strip().lower()
        if status == "draft":
            competitions = competitions.filter(
                started_at__isnull=True, planned_for__isnull=True
            )
        elif status == "scheduled":
            competitions = competitions.filter(
                planned_for__isnull=False, started_at__isnull=True
            )
        elif status == "running":
            from competitions.models import ACTIVE_STATES, CompetitionState

            competitions = competitions.filter(
                started_at__isnull=False, state__in=list(ACTIVE_STATES)
            ).exclude(state=CompetitionState.FINISHED)
        elif status == "completed":
            from competitions.models import CompetitionState

            competitions = competitions.filter(state=CompetitionState.FINISHED)

        context.update({
            "competitions": competitions,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
            "status_filter": status,
        })
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class CompetitionDetailView(TemplateView):
    """Detail view for a competition."""

    template_name = "teacher/competition_detail.html"
    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Get the competition - ensure user has permission
        competition = get_object_or_404(Competition, pk=self.kwargs["pk"])

        # Permission check: teacher must own it, admin can see all
        if not is_administrator(user) and competition.teacher != user:
            raise Http404("Competition not found.")

        # The detail page and the start form are one template, so they read one
        # context. Building it here is what lets the Start Competition button's
        # offline-screen warning appear on the detail page at all.
        context.update(_competition_page_context(self.request, competition))
        return context

    def get_login_url(self) -> str:
        return reverse("accounts:login")


def _competition_page_context(request, competition: Competition) -> dict:
    """Everything the competition detail page and its Start form read.

    Both surfaces share one builder because they share one template: the detail
    page carries the Start Competition button, and the form it sits next to
    decides, from this context, whether to warn about offline screens and require
    an acknowledgement. When the two views built their own context separately the
    detail page supplied `targeting` only, and the start form's offline-screen
    warning silently never rendered on it.
    """
    targeting = screen_targeting.for_competition(competition)
    user = request.user
    return {
        "competition": competition,
        "targeting": targeting,
        "is_administrator": is_administrator(user),
        "is_teacher": user.is_teacher,
        "role_display": user.get_role_display(),
        "start_confirmation": False,
        # Every question and classroom must be present before a round can run,
        # so the button names exactly what would fail.
        "start_blockers": start_blockers(competition),
        "has_offline_screens": bool(targeting.offline_screens),
        "has_classrooms_without_screens": bool(targeting.classrooms_without_screens),
    }


class StartCompetitionView(View):
    """The Start Competition button.

    A POST, and only a POST: starting a round is a deliberate act that changes
    what every screen in the school shows, and something that consequential must
    not be reachable by following a link. The confirmation is a field on the same
    form rather than a second page, so the teacher sees what they are about to do
    and then does it, without a reload in between losing their place.

    The state change goes through the live engine - `assert_can_control` then
    `start_competition` - so this view authorises, checks and dispatches, and
    never writes state itself.
    """

    http_method_names = ["get", "post"]

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_staff_member(request.user):
            raise Http404("This account has no assigned role.")
        return super().dispatch(request, *args, **kwargs)

    def _competition(self, pk) -> Competition:
        competition = get_object_or_404(Competition, pk=pk)
        if not is_administrator(self.request.user) and competition.teacher != self.request.user:
            # Another teacher's competition, or one given away. The screen
            # targeting is partly derived from classrooms this user may not read,
            # so the safe answer is the same one the detail page gives.
            raise Http404("Competition not found.")
        return competition

    def get(self, request, *args, **kwargs):
        """The confirmation, in the detail page's own layout."""
        competition = self._competition(kwargs["pk"])
        context = _competition_page_context(request, competition)
        context["start_confirmation"] = True
        return render(request, "teacher/competition_detail.html", context)

    def post(self, request, *args, **kwargs):
        competition = self._competition(kwargs["pk"])

        blockers = start_blockers(competition)
        if blockers:
            messages.error(
                request,
                _("This competition is not ready to start."),
            )
            context = _competition_page_context(request, competition)
            return render(
                request, "teacher/competition_detail.html", context
            )

        # A screen that will miss the round is not a reason to refuse: the teacher
        # may be about to start without it. But it must be said out loud and
        # acknowledged, rather than the start being reported as fully delivered.
        requires_ack = (
            screen_targeting.for_competition(competition).offline_screens
            and not request.POST.get("acknowledge_screens")
        )
        if requires_ack:
            messages.warning(
                request,
                _("Some expected screens are offline. Confirm to start anyway."),
            )
            context = _competition_page_context(request, competition)
            return render(
                request, "teacher/competition_detail.html", context
            )

        try:
            from channels.layers import get_channel_layer

            from live.publish import publish_transition_sync

            transition = start_competition(competition)
            publish_transition_sync(get_channel_layer(), transition)
        except CompetitionControlError as exc:
            # A second press, a race with a clock transition, or a start on
            # something already finished: refused, with the reason named.
            messages.error(request, exc.message)
            context = _competition_page_context(request, competition)
            return render(
                request, "teacher/competition_detail.html", context
            )

        messages.success(
            request,
            _("%(title)s is live. The screens in the participating classrooms are now showing it.")
            % {"title": competition.title},
        )
        return redirect("live:teacher_dashboard", competition_id=competition.pk)


class PlanCompetitionView(View):
    """Record when the teacher intends to run a competition.

    A note, not a trigger. This writes `planned_for` and nothing else: the
    competition's state is untouched, it does not start, and no event is sent to
    any screen. The lifecycle label on the detail page then says "waiting for
    teacher to start", which is the honest description of what happens next.
    """

    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_staff_member(request.user):
            raise Http404("This account has no assigned role.")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        competition = get_object_or_404(Competition, pk=kwargs["pk"])
        if not is_administrator(request.user) and competition.teacher != request.user:
            raise Http404("Competition not found.")

        raw = (request.POST.get("planned_for") or "").strip()
        if not raw:
            competition.planned_for = None
            competition.save(update_fields=["planned_for", "updated_at"])
            messages.info(request, _("Planned time cleared."))
            return redirect("teacher:competition_detail", pk=competition.pk)

        parsed = parse_datetime(raw)
        if parsed is None:
            messages.error(
                request,
                _("Enter the date and time in the format YYYY-MM-DD HH:MM."),
            )
            return redirect("teacher:competition_detail", pk=competition.pk)

        competition.planned_for = parsed
        competition.save(update_fields=["planned_for", "updated_at"])
        if timezone.is_naive(parsed):
            competition.planned_for = parsed.replace(tzinfo=timezone.get_current_timezone())
            competition.save(update_fields=["planned_for", "updated_at"])
        messages.success(
            request,
            _("Planned for %(when)s. This competition will not start until you press Start Competition.")
            % {"when": parsed.strftime("%Y-%m-%d %H:%M")},
        )
        return redirect("teacher:competition_detail", pk=competition.pk)


def start_blockers(competition) -> list[str]:
    """What would stop this competition from being started.

    Returned as translatable strings rather than booleans, because the message a
    teacher needs is "which three things are missing", not "not startable".
    """
    from django.utils.translation import gettext_lazy as _

    blockers: list[str] = []
    if competition.questions.count() == 0:
        blockers.append(_("There are no questions in this competition."))
    if competition.classrooms.count() == 0:
        blockers.append(_("There are no classrooms taking part."))
    return blockers


class QuestionBankView(TemplateView):
    """The teacher's question bank.

    Questions are a **shared bank** organised by subject, not a per-teacher
    collection: this deployment's existing convention, and the least-privilege
    reading that keeps a question reusable by whoever teaches that subject next
    term. Teachers create, view, edit and retire questions here; nobody needs
    Django Admin, and nothing here exposes a raw JSON field.

    Every filter the bank offers is applied here, in Python, from validated
    query parameters - the template renders the controls, and this view decides
    what they mean. An unrecognised parameter is ignored rather than trusted, so
    a malformed link shows a valid page instead of a 500.
    """

    template_name = "teacher/questions.html"
    paginate_by = 20

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        """The bank, plus the two POST actions it hosts.

        Retiring and restoring a question is a state change, so it is a POST
        rather than a link: a GET that changed data could be triggered by a
        search-engine crawler or an image tag prefetch, which is how a live
        question ended up inactive before anyone touched it.
        """
        if request.method == "POST":
            return self._toggle(request)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self._toggle(request)

    def _toggle(self, request: HttpRequest):
        """Retire a question from the bank, or restore it.

        Retiring keeps the question in the database with ``active=False``: it
        still exists, it stays out of new competitions, and it can be restored in
        one click. Deleting it would destroy the record of every competition
        that ever used it.
        """
        question = get_object_or_404(Question, pk=request.POST.get("question_id", ""))
        if not is_staff_member(request.user):
            raise Http404("This account has no assigned role.")

        question.active = not question.active
        question.save(update_fields=["active", "updated_at"])
        messages.success(
            request,
            _('Question "%(text)s" is now active in the bank.')
            % {"text": question.text[:60]}
            if question.active
            else _('Question "%(text)s" was retired from the bank.')
            % {"text": question.text[:60]},
        )
        return redirect(request.POST.get("next") or "teacher:questions")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request

        # The whole bank, including retired questions: a teacher must be able to
        # find and restore something they retired, and a filter that hid
        # inactive rows made that impossible.
        questions = Question.objects.select_related("subject", "topic").order_by(
            "-created_at"
        )
        total_count = questions.count()

        # Subject first: it is the bank's organisation, and the only filter that
        # narrows the topic list below it. An unknown id is a stale link or a
        # typo, so it is ignored rather than passed to the database.
        subjects = Subject.objects.order_by("order", "name")
        selected_subject = self._parsed_id(request.GET.get("subject"))
        if selected_subject and subjects.filter(pk=selected_subject).exists():
            questions = questions.filter(subject_id=selected_subject)

        # Topics depend on the chosen subject, so the list narrows with it. With
        # no subject chosen, every topic in the bank is offered.
        if selected_subject:
            topics = Topic.objects.filter(subject_id=selected_subject).order_by(
                "order", "name"
            )
        else:
            topics = Topic.objects.order_by("subject__order", "order", "name")
        selected_topic = self._parsed_id(request.GET.get("topic"))
        if selected_topic and topics.filter(pk=selected_topic).exists():
            questions = questions.filter(topic_id=selected_topic)

        selected_type = (request.GET.get("type") or "").strip()
        if selected_type and selected_type in dict(QuestionType.choices):
            questions = questions.filter(question_type=selected_type)

        # The default is "active only", which is what the bank has always shown;
        # the other options exist so a teacher can find the retired ones.
        selected_status = (request.GET.get("status") or "active").strip()
        if selected_status == "active":
            questions = questions.filter(active=True)
        elif selected_status == "inactive":
            questions = questions.filter(active=False)

        # Free text over the question itself.
        query = (request.GET.get("q") or "").strip()
        if query:
            questions = questions.filter(text__icontains=query)

        questions = questions.order_by("-created_at")

        # Pagination keeps the page usable when a bank grows: the school's real
        # bank is small today, and a teacher who adds fifty questions should not
        # have to scroll through all of them to reach the newest.
        paginator = Paginator(questions, self.paginate_by)
        page_number = request.GET.get("page") or "1"
        try:
            page = paginator.page(page_number)
        except PageNotAnInteger:
            page = paginator.page(1)
        except EmptyPage:
            page = paginator.page(paginator.num_pages)

        context.update({
            "questions": page,
            "page_obj": page,
            "paginator": paginator,
            "is_paginated": page.has_other_pages(),
            "total_count": total_count,
            "shown_count": paginator.count,
            "subject_choices": subjects,
            "topic_choices": topics,
            "question_type_choices": QuestionType.choices,
            "selected_subject": selected_subject or "",
            "selected_topic": selected_topic or "",
            "selected_type": selected_type,
            "selected_status": selected_status,
            "query": query,
        })
        return context

    @staticmethod
    def _parsed_id(raw) -> int | None:
        """A query parameter as a positive integer, or ``None`` if it is not one.

        Query strings are attacker-controlled: a non-numeric id is discarded
        here rather than reaching the ORM, where it would raise ``ValueError``.
        """
        raw = (str(raw) if raw is not None else "").strip()
        if not raw.isdigit():
            return None
        value = int(raw)
        return value or None

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class QuestionDetailView(TemplateView):
    """One question, read only.

    A question's answer key is the part a teacher must be able to check without
    editing it, and the part the preview deliberately hides. So the bank links
    here to see what is stored, and the edit form is the only place it changes.
    """

    template_name = "teacher/question_detail.html"

    @_staff_required
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        question = get_object_or_404(Question, pk=self.kwargs["pk"])
        context.update({
            "question": question,
            # The preview partial expects the same shape the builder's seeded
            # state uses, so build it here rather than letting the partial reach
            # into a model it does not know about.
            "question_as_preview": {
                "question_type": question.question_type,
                "text": question.text,
                "options": list(question.options or []),
                "correct_option": question.correct_option or "",
                "correct_answer": question.correct_answer,
                "type_config": dict(question.type_config or {}),
                "duration_seconds": question.duration_seconds,
                "active": question.active,
                "hint": question.hint or "",
                "image_url": question.image.url if question.image else "",
            },
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
            messages.success(
                request, _('Question "%(text)s" saved.') % {"text": question.text[:60]}
            )
            if save_option == "save_another":
                return redirect("teacher:create_question")
            if save_option == "save_to_competition":
                # Straight back to the wizard, carrying only the new question's
                # id. The wizard's own state is in the session, so it ticks this
                # question and leaves everything else the teacher had entered
                # alone - which is what makes the detour safe.
                url = reverse("teacher:create_competition")
                return redirect(f"{url}?{urlencode({'question_id': question.pk})}")
            return redirect("teacher:questions")
        context = self._build_context(form, None, save_option, is_edit=False)
        return render(request, self.template_name, context)

    def _build_context(self, form, question, save_option, *, is_edit):
        user = self.request.user
        # The builder's JavaScript seeds its rows from this state, so it must
        # carry the question being edited. Without it the builder falls back to
        # "Option A / Option B / Item 1" placeholders and, on save, overwrites a
        # question's real options and answer key with the placeholders - which is
        # why a teacher editing a question appeared to lose their data.
        builder_data = self._builder_state(form, question, is_edit=is_edit)
        context = {
            "form": form,
            "question": question,
            "question_types": QuestionType.choices,
            "save_option": save_option,
            "is_edit": is_edit,
            "preview_data": builder_data,
            "builder_data": builder_data,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        }
        return context

    @staticmethod
    def _builder_state(form, question, *, is_edit: bool) -> dict:
        """The question as the builder should see it, for seeding its controls.

        A bound, valid form reports what the teacher just typed, so the rows the
        JS builds match the POST that is about to be sent. Otherwise an edit is
        seeded from the stored question, and a new question from the same
        defaults the blank form shows. In every case this must be a complete
        object: the builder reads `state.options`, `state.type_config` and so on,
        and an absent key silently becomes the placeholder row.
        """
        if form.is_bound and form.is_valid():
            return QuestionCreateView._build_preview(form.cleaned_data)
        if is_edit and question is not None:
            return {
                "question_type": question.question_type,
                "text": question.text,
                "subject_id": question.subject_id,
                "topic_id": question.topic_id,
                "options": list(question.options or []),
                "correct_option": question.correct_option or "",
                "correct_answer": question.correct_answer,
                "type_config": dict(question.type_config or {}),
                "duration_seconds": question.duration_seconds,
                "active": question.active,
                "hint": question.hint or "",
                "explanation": question.explanation or "",
                "image_url": question.image.url if question.image else "",
            }
        return {
            "question_type": QuestionType.MULTIPLE_CHOICE,
            "text": "",
            "options": [],
            "correct_option": "",
            "correct_answer": None,
            "type_config": {},
            "duration_seconds": 30,
            "active": True,
        }

    @classmethod
    def _build_preview(cls, cleaned_data: dict) -> dict:
        state = {
            "question_type": cleaned_data.get("question_type"),
            "text": cleaned_data.get("text", ""),
            "options": list(cleaned_data.get("_parsed_options", []) or []),
            "correct_option": cleaned_data.get("correct_option") or "",
            "correct_answer": cleaned_data.get("_parsed_correct_answer"),
            "type_config": dict(cleaned_data.get("_parsed_type_config", {}) or {}),
            "duration_seconds": cleaned_data.get("duration_seconds", 30),
            "active": cleaned_data.get("active", True),
            "subject_id": getattr(cleaned_data.get("subject"), "pk", None),
            "topic_id": getattr(cleaned_data.get("topic"), "pk", None),
        }
        return state

    def get_login_url(self) -> str:
        return reverse("accounts:login")


class QuestionEditView(QuestionCreateView):
    """Edit an existing question using the unified Question Builder.

    It inherits from the create view so the builder itself - the seeded state,
    the live preview, the shared context - is one implementation. A separate
    copy of this view is how editing a question came to drop its options and
    answer key: the copy supplied `preview_data` only, the template reads
    `builder_data`, and the builder had to fall back to placeholder rows.
    """

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

        # An administrator reviews every championship in the school; a teacher sees
        # only the ones they created. `for_user` encodes that one rule.
        tournaments = Tournament.objects.for_user(user).order_by("-created_at")

        context.update({
            "tournaments": tournaments,
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
        })
        return context

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
        user = self.request.user
        pk = self.kwargs["pk"]

        # Get the tournament - ensure user has permission
        tournament = get_object_or_404(Tournament, pk=pk)

        # Permission check: teacher must own it, admin can see all
        if not is_administrator(user) and tournament.created_by != user:
            raise Http404("Tournament not found.")

        # The detail page reads the participants, the stages and (for a concluded
        # championship) the frozen record off the context, not off the tournament
        # alone: they are three separate queries with ordering rules, and asking
        # the template to walk them would make the view body do queries the
        # reader cannot see.
        context.update({
            "tournament": tournament,
            "participants": tournament.participants.select_related(
                "classroom", "current_stage", "eliminated_at_stage"
            ).order_by("classroom__name"),
            "stages": tournament.stages.prefetch_related("competitions__competition").order_by(
                "order"
            ),
            "record": getattr(tournament, "record", None),
            "history": services.history_for(tournament),
            "is_administrator": is_administrator(user),
            "is_teacher": user.is_teacher,
            "role_display": user.get_role_display(),
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


def set_language(request, *args, **kwargs):
    """Switch the teacher's interface language, through Django's own view.

    The masthead posts here with a ``language`` code. Two things happen:

    1. The code is checked against ``settings.LANGUAGES``. This project offers
       English and Arabic and nothing else. Django's own view uses
       ``check_for_language``, which accepts any language for which *any*
       translation file on the system exists - including the ones bundled inside
       Django for a hundred other locales. Left alone, a ``fr`` would flip the
       chrome into French while every project string stayed English.
    2. The real work is delegated to ``django.views.i18n.set_language``: it
       validates the ``next`` URL against the request host, sets the language
       cookie, and redirects. That cookie is what the next request reads.
       Django's ``LocaleMiddleware`` reads it in
       ``translation.get_language_from_request`` and activates it before the
       view runs, so the whole interface - templates, forms, model status
       labels, date formatting - is translated from that point on.

    When (1) rejects the code the request never reaches Django's view, so no
    cookie is written and the screen stays in the language it was.
    """
    from django.views.i18n import LANGUAGE_QUERY_PARAMETER, set_language as django_set_language

    # Skip the guard on a GET: Django's view ignores it and only redirects, so
    # there is nothing to protect.
    if request.method == "POST":
        code = request.POST.get(LANGUAGE_QUERY_PARAMETER, "")
        if code not in dict(settings.LANGUAGES):
            messages.info(
                request,
                _("That language is not available on this system."),
            )
            return redirect(request.POST.get("next") or reverse("teacher:dashboard"))

    return django_set_language(request, *args, **kwargs)
