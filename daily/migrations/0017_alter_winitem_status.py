from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("daily", "0016_coachsuggestion_base_questions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="winitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("done", "Done"),
                    ("graduated", "Graduated to habit"),
                    ("archived", "Archived"),
                ],
                default="open",
                max_length=12,
            ),
        ),
    ]
