from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("questions", "0003_question_explanation_question_type_config_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="question",
            name="hint",
            field=models.TextField(
                blank=True,
                help_text="Optional clue for the teacher to share while asking the question.",
            ),
        ),
    ]
