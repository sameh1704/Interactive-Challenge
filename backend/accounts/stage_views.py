"""Read-only monitoring; never expose a Screen ID or use global report builders."""
from django.contrib.auth.views import redirect_to_login
from django.db.models import Count, Sum, F
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _
from django.views import View
from accounts.permissions import is_stage_deputy
from accounts.stage_scope import classrooms_for_deputy, competitions_for_deputy
from classrooms.models import SchoolStage
from reports.exports import csv_response
from scoring.models import Answer, CompetitionResult
from screens.models import InteractiveScreen


class DeputyView(View):
    http_method_names = ['get', 'head', 'options']

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_stage_deputy(request.user):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, kind='dashboard', pk=None):
        stages = SchoolStage.objects.filter(active=True, deputies=request.user)
        rooms = classrooms_for_deputy(request.user).select_related('stage')
        rounds = competitions_for_deputy(request.user).order_by('-created_at')
        # A forged filter is a refusal, never proof of authorization.
        if request.GET.get('stage'):
            raw = request.GET['stage']
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 19 or int(raw) > 9223372036854775807:
                raise Http404
            stage = get_object_or_404(stages, pk=raw)
            rooms = rooms.filter(stage=stage)
            rounds = rounds.filter(classrooms__classroom__in=rooms).distinct()
        if request.GET.get('q'):
            rooms = rooms.filter(name__icontains=request.GET['q'][:200])
        screens = InteractiveScreen.objects.filter(classroom__in=rooms).select_related('classroom')
        selected = None
        if kind == 'classrooms':
            selected = get_object_or_404(rooms, pk=pk)
            screens = screens.filter(classroom=selected)
            rounds = rounds.filter(classrooms__classroom=selected)
        elif kind == 'screens':
            selected = get_object_or_404(screens, pk=pk)
            rooms = rooms.filter(pk=selected.classroom_id)
            screens = screens.filter(pk=pk)
            rounds = rounds.filter(classrooms__classroom_id=selected.classroom_id)
        elif kind == 'competitions':
            selected = get_object_or_404(rounds, pk=pk)
            rooms = rooms.filter(pk__in=selected.classrooms.values('classroom_id'))
            screens = screens.filter(classroom__in=rooms)
            rounds = rounds.filter(pk=pk)
        status = request.GET.get('status', '')
        counts = dict(active_count=rounds.filter(started_at__isnull=False).exclude(state='finished').count(),
            scheduled_count=rounds.filter(started_at__isnull=True, planned_for__isnull=False).count(),
            completed_count=rounds.filter(state='finished').count())
        if status == 'scheduled':
            rounds = rounds.filter(started_at__isnull=True, planned_for__isnull=False)
        elif status == 'active':
            rounds = rounds.filter(started_at__isnull=False).exclude(state='finished')
        elif status == 'completed':
            rounds = rounds.filter(state='finished')
        elif status:
            raise Http404
        results = stage_results(rounds, rooms)
        if kind == 'export':
            def cell(value):
                return "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value
            return csv_response('stage-results.csv',
                [_('Competition'), _('Classroom'), _('Score'), _('Answers')],
                [[cell(x[k]) for k in ('title', 'classroom', 'score', 'answers')] for x in results])
        context = dict(stages=stages, rooms=Paginator(rooms.order_by('pk'), 25).get_page(request.GET.get('rooms_page')),
            screens=Paginator(screens.order_by('pk'), 25).get_page(request.GET.get('screens_page')),
            rounds=Paginator(rounds, 25).get_page(request.GET.get('page')),
            results=Paginator(results, 25).get_page(request.GET.get('results_page')), selected=selected,
            selected_label=getattr(selected, 'name', getattr(selected, 'title', '')), classroom_count=rooms.count(),
            screen_count=screens.count(), online_count=screens.online().count(),
            offline_count=screens.count()-screens.online().count(),
            competition_count=rounds.count(), **counts, stage_filter=request.GET.get('stage', ''),
            status_filter=status, query=request.GET.get('q', ''))
        return render(request, 'accounts/deputy.html', context)


def stage_results(rounds, rooms):
    """Prefer immutable final snapshots; never render their untrusted room names."""
    allowed = dict(rooms.values_list('pk', 'name'))
    snapshots = CompetitionResult.objects.filter(competition__in=rounds).select_related('competition')
    rows = []
    frozen = []
    for snapshot in snapshots:
        frozen.append(snapshot.competition_id)
        for entry in snapshot.standings:
            room_id = entry.get('classroom_id')
            if room_id in allowed:
                rows.append(dict(competition_id=snapshot.competition_id, title=snapshot.competition.title,
                    classroom=allowed[room_id], score=entry.get('score', 0), answers=entry.get('answers_given', 0)))
    answers = Answer.objects.filter(competition__in=rounds, classroom__in=rooms).exclude(competition_id__in=frozen)
    for row in answers.values('competition_id', 'competition__title', 'classroom_id', 'classroom__name').annotate(
        score=Sum(F('points') + F('speed_bonus')), answers=Count('pk')):
        rows.append(dict(competition_id=row['competition_id'], title=row['competition__title'],
            classroom=row['classroom__name'], score=row['score'], answers=row['answers']))
    return sorted(rows, key=lambda x: (x['competition_id'], x['classroom']))
