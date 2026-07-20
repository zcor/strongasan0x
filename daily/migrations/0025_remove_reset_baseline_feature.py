from django.db import migrations, models


def remove_reset_setting(apps, schema_editor):
    DailyParticipant = apps.get_model("daily", "DailyParticipant")
    for participant in DailyParticipant.objects.exclude(legacy_health_config__isnull=True):
        config = participant.legacy_health_config
        if isinstance(config, dict) and "reset" in config:
            config.pop("reset")
            participant.legacy_health_config = config
            participant.save(update_fields=["legacy_health_config"])


class Migration(migrations.Migration):

    dependencies = [
        ("daily", "0024_dailybetarollout_alter_dailyparticipant_beta"),
    ]

    operations = [
        migrations.RunPython(remove_reset_setting, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="checklistversion",
            name="source",
            field=models.CharField(
                choices=[
                    ("baseline", "Baseline (Stronger in 60)"),
                    ("ai_mutation", "AI mutation"),
                ],
                max_length=20,
            ),
        ),
    ]
