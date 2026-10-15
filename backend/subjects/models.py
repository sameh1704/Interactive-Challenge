"""The school's subjects and the optional topics inside them.

These are two different things and are kept apart on purpose:

* A **subject** is the curriculum subject - Mathematics, Science, Arabic,
  English, Social Studies. It is what a teacher picks first, and what a question
  belongs to. The names are managed centrally by administrators, so a teacher
  choosing "Science" and a teacher choosing "علوم" are looking at the same
  subject rather than two spellings of it.
* A **topic** is an optional subdivision *within* a subject - "Fractions" inside
  Mathematics. It narrows a bank without being a bank of its own.

Neither is a question and neither is a competition. A question belongs to a
subject; a competition selects questions.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class SubjectQuerySet(models.QuerySet):
    def active(self) -> "SubjectQuerySet":
        return self.filter(active=True)

    def ordered(self) -> "SubjectQuerySet":
        return self.order_by("order", "name")


class Subject(models.Model):
    """A curriculum subject, e.g. Mathematics or Science.

    ``slug`` is the stable identity for anything durable - a saved competition
    that remembers which subject it was built from - and ``name`` is what a
    human reads, so renaming the display does not orphan records.
    """

    name = models.CharField(max_length=100, help_text="The subject as staff read it.")
    slug = models.SlugField(
        max_length=120,
        unique=True,
        help_text="Stable identity used by saved links and migrations.",
    )
    description = models.TextField(blank=True, help_text="Optional note for other staff.")
    order = models.PositiveIntegerField(
        default=0, help_text="Display order; smaller comes first."
    )
    active = models.BooleanField(
        default=True, help_text="Inactive subjects are hidden from new questions."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SubjectQuerySet.as_manager()

    class Meta:
        verbose_name = "subject"
        verbose_name_plural = "subjects"
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.name


class TopicQuerySet(models.QuerySet):
    def active(self) -> "TopicQuerySet":
        return self.filter(active=True)


class Topic(models.Model):
    """An optional subdivision within a subject.

    A question may belong to a subject with no topic at all: topics are a way to
    narrow a large bank, not a requirement. Kept in its own model rather than as
    a text field on the question, so a teacher picking "Fractions" is picking the
    same topic another teacher means.
    """

    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="topics",
        help_text="The subject this topic narrows.",
    )
    name = models.CharField(max_length=100, help_text="The topic as staff read it.")
    slug = models.SlugField(
        max_length=120,
        help_text="Stable identity within its subject.",
    )
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TopicQuerySet.as_manager()

    class Meta:
        verbose_name = "topic"
        verbose_name_plural = "topics"
        ordering = ["subject__order", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "slug"], name="subjects_topic_unique_within_subject"
            )
        ]

    def __str__(self) -> str:
        return f"{self.subject.name} › {self.name}"
