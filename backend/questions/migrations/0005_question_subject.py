"""Give every question a subject, without losing any.

Runs in three steps inside one migration, and the order matters:

1. ``AddField`` with ``null=True``. The column has to exist and be nullable
   before anything can be written into it, and any question the migration is
   about to leave alone must not be rejected by a NOT NULL constraint first.
2. A data migration that creates one default subject and points every existing
   question at it. Nothing is deleted, nothing is guessed per-question: an
   unsorted bank lands in one place a teacher can then re-file by hand, which is
   the only honest option - the migration cannot know which subject a question
   belongs to, because that information was never recorded.
3. ``AlterField`` to ``null=False``. Only reached once the step before it has
   left no row behind, because a migration is atomic: if the backfill were to
   fail, the whole thing rolls back and the schema is untouched rather than left
   requiring a subject that some question does not have.
"""

import django.db.models.deletion
from django.db import migrations, models

DEFAULT_SUBJECT_NAME = "General"
DEFAULT_SUBJECT_SLUG = "general"


def create_default_and_backfill(apps, schema_editor) -> None:
    Subject = apps.get_model("subjects", "Subject")
    Question = apps.get_model("questions", "Question")

    subject, _created = Subject.objects.get_or_create(
        slug=DEFAULT_SUBJECT_SLUG,
        defaults={"name": DEFAULT_SUBJECT_NAME, "order": 0, "active": True},
    )

    # Every existing question, and only the ones without a subject: a re-run
    # must not move a question that has already been re-filed by hand.
    moved = Question.objects.filter(subject__isnull=True).update(subject=subject)
    if moved:
        print(f"  assigned {moved} existing question(s) to '{subject.name}'")


def noop(apps, schema_editor) -> None:
    """The reverse of a backfill.

    There is nothing sensible to undo: dropping the subject would orphan every
    question that was re-filed by hand, and this migration's whole job was not to
    orphan them in the first place. Reverse by hand if that is ever needed.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("subjects", "0001_initial"),
        ("questions", "0004_question_hint"),
    ]

    operations = [
        migrations.AddField(
            model_name="question",
            name="subject",
            field=models.ForeignKey(
                help_text="The curriculum subject this question belongs to.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="questions",
                to="subjects.subject",
            ),
        ),
        migrations.AddField(
            model_name="question",
            name="topic",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional subdivision within the subject.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="questions",
                to="subjects.topic",
            ),
        ),
        migrations.RunPython(create_default_and_backfill, noop),
        migrations.AlterField(
            model_name="question",
            name="subject",
            field=models.ForeignKey(
                help_text="The curriculum subject this question belongs to.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="questions",
                to="subjects.subject",
            ),
        ),
    ]
