from django.db import migrations, models


class Migration(migrations.Migration):
    """Record the intended start time of a competition.

    ``planned_for`` is a note, not a state: nothing reads it to change anything.
    A round is live when ``started_at`` is set, which only the teacher's Start
    button writes.
    """

    dependencies = [
        ("competitions", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="competition",
            name="planned_for",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "When the teacher intends to run this competition. Scheduling a "
                    "time does not start it; only the Start button does."
                ),
                null=True,
            ),
        ),
    ]
