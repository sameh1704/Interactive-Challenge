"""Fail-closed stage visibility, shared by monitoring pages and querysets."""
from django.db.models import Exists, OuterRef
from accounts.permissions import is_stage_deputy
from classrooms.models import Classroom
from competitions.models import Competition, CompetitionClassroom


def classrooms_for_deputy(user):
    if not is_stage_deputy(user):
        return Classroom.objects.none()
    return Classroom.objects.filter(stage__active=True, stage__deputies=user).distinct()


def competitions_for_deputy(user, queryset=None):
    qs = queryset if queryset is not None else Competition.objects.all()
    if not is_stage_deputy(user):
        return qs.none()
    targets = CompetitionClassroom.objects.filter(competition_id=OuterRef('pk'))
    outside = targets.exclude(classroom__in=classrooms_for_deputy(user))
    # A mixed-stage round can expose opponents and their results. Require every
    # target, including inactive classrooms, to be explicitly within scope.
    return qs.filter(Exists(targets), ~Exists(outside))
