import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies: list = []

    operations = [
        migrations.CreateModel(
            name="Subject",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(help_text="The subject as staff read it.", max_length=100)),
                (
                    "slug",
                    models.SlugField(
                        help_text="Stable identity used by saved links and migrations.",
                        max_length=120,
                        unique=True,
                    ),
                ),
                ("description", models.TextField(blank=True, help_text="Optional note for other staff.")),
                ("order", models.PositiveIntegerField(default=0, help_text="Display order; smaller comes first.")),
                ("active", models.BooleanField(default=True, help_text="Inactive subjects are hidden from new questions.")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "subject",
                "verbose_name_plural": "subjects",
                "ordering": ["order", "name"],
            },
        ),
        migrations.CreateModel(
            name="Topic",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(help_text="The topic as staff read it.", max_length=100)),
                ("slug", models.SlugField(help_text="Stable identity within its subject.", max_length=120)),
                ("description", models.TextField(blank=True)),
                ("order", models.PositiveIntegerField(default=0)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "subject",
                    models.ForeignKey(
                        help_text="The subject this topic narrows.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="topics",
                        to="subjects.subject",
                    ),
                ),
            ],
            options={
                "verbose_name": "topic",
                "verbose_name_plural": "topics",
                "ordering": ["subject__order", "order", "name"],
            },
        ),
        migrations.AddConstraint(
            model_name="topic",
            constraint=models.UniqueConstraint(
                fields=("subject", "slug"), name="subjects_topic_unique_within_subject"
            ),
        ),
    ]
