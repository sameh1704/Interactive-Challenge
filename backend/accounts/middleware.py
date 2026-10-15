"""A deputy has a small, explicit HTTP surface, including on legacy routes."""
from django.http import HttpResponseNotFound
from django.shortcuts import redirect
from accounts.permissions import is_stage_deputy


class StageDeputyBoundaryMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if is_stage_deputy(getattr(request, 'user', None)):
            if request.path_info in ('/', '/dashboard/', '/teacher/', '/workspace/'):
                return redirect('deputy:dashboard')
            allowed = (
                request.path_info.startswith(('/deputy/', '/static/'))
                or request.path_info in (
                    '/accounts/login/', '/accounts/logout/', '/i18n/setlang/',
                    '/teacher/set_language/', '/theme/', '/health/',
                )
            )
            if not allowed:
                return HttpResponseNotFound()
        return self.get_response(request)
