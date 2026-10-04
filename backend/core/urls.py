"""URL routes owned by the core app."""

from __future__ import annotations

from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("health/", views.health, name="health"),
]