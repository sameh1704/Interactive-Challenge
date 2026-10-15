from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("competitions", "0005_competition_planned_for"),
    ]

    operations = [
        migrations.AddField(
            model_name="competitionquestion",
            name="points",
            field=models.PositiveIntegerField(
                blank=True,
                help_text=(
                    "What a correct answer to this question is worth in this round. "
                    "Leave blank to use the competition's points per question."
                ),
                null=True,
            ),
        ),
    ]
