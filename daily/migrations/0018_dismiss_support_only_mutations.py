from django.db import migrations
from django.utils import timezone


def dismiss_support_only_mutations(apps, schema_editor):
    CoachSuggestion = apps.get_model("daily", "CoachSuggestion")
    CoachSuggestion.objects.filter(
        check_in__participant__beta=True,
        check_in__participant__ai_mutations_enabled=False,
        proposed_questions__isnull=False,
        status="pending",
    ).update(status="dismissed", responded_at=timezone.now())


class Migration(migrations.Migration):

    dependencies = [
        ("daily", "0017_alter_winitem_status"),
    ]

    operations = [
        migrations.RunPython(
            dismiss_support_only_mutations,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
