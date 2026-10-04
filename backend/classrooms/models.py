"""The Classroom model."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.urls import reverse


class ClassroomQuerySet(models.QuerySet):
    """Reusable lookups for the school-facing views."""

    def active(self):
        return self.filter(active=True)

    def for_user(self, user):
        """Classrooms a staff member is allowed to see.

        Administrators see every classroom; teachers see only their assignments.
        Anonymous users see nothing.
        """
        if not user or not user.is_authenticated or not user.is_active:
            return self.none()
        if user.is_administrator:
            return self
        return self.filter(teachers=user)


class Classroom(models.Model):
    """A physical classroom that can take part in a competition.

    ``grade`` and ``section`` are free text rather than a fixed enumeration
    because schools organise cohorts differently, and because competition
    grouping rules are not settled yet. They are indexed so that filtering by
    grade stays cheap once the question bank exists.
    """

    name = models.CharField(
        max_length=120,
        unique=True,
        help_text="Unique classroom name, for example 'Science Lab 1'.",
    )
    grade = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="For example 'Grade 7' or 'Year 2'. Free text.",
    )
    section = models.CharField(max_length=64, blank=True)
    building = models.CharField(max_length=120, blank=True)
    floor = models.CharField(max_length=32, blank=True)
    room_number = models.CharField(max_length=32, blank=True)
    active = models.BooleanField(
        default=True,
        help_text="Inactive classrooms are hidden from selection but keep their history.",
    )
    teachers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="classrooms",
        blank=True,
        verbose_name="Teachers",
        help_text="Teachers who may see this classroom on their dashboard.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ClassroomQuerySet.as_manager()

    class Meta:
        verbose_name = "classroom"
        verbose_name_plural = "classrooms"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("admin:classrooms_classroom_change", args=[self.pk])

    @property
    def display_name(self) -> str:
        """Human-friendly label used on screens, dashboards and previews."""
        label = self.name
        if self.section:
            label = f"{label} ({self.section})"
        if self.grade and self.grade.casefold() not in self.name.casefold():
            label = f"{self.grade} - {label}"
        return label

    @property
    def location(self) -> str:
        """Location parts joined for display, e.g. 'Main Building, Floor 2, Room 201'."""
        return ", ".join(
            part for part in (self.building, self.floor, self.room_number) if part
        )