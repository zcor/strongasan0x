"""Tag bonuses created after the earlier one-time health backfills.

The frozen dashboard continued generating untagged health bonuses between the
0021 backfill and a participant's beta cutover. Runtime writes now add the tag;
this final pass closes the existing-data gap so those bonuses remain visible
when beta's explicit health filter turns on.
"""

from django.db import migrations


def _tag(items):
    changed = False
    for item in items or []:
        if isinstance(item, dict) and "category" not in item:
            item["category"] = "health"
            changed = True
    return changed


def tag_post_grandfather_bonuses(apps, schema_editor):
    ChecklistVersion = apps.get_model("daily", "ChecklistVersion")
    for version in ChecklistVersion.objects.exclude(bonus_questions=None).iterator():
        if _tag(version.bonus_questions):
            version.save(update_fields=["bonus_questions"])

    # A legacy overnight suggestion can be queued but not applied yet when the
    # participant is switched. Tag that stored proposal too, otherwise beta's
    # strict apply-time filter would discard a legitimate pending health bonus.
    CoachSuggestion = apps.get_model("daily", "CoachSuggestion")
    for suggestion in CoachSuggestion.objects.filter(
        status="pending",
    ).exclude(proposed_bonus=None).iterator():
        if _tag(suggestion.proposed_bonus):
            suggestion.save(update_fields=["proposed_bonus"])


class Migration(migrations.Migration):

    dependencies = [
        ("daily", "0022_dailyparticipant_legacy_health_config"),
    ]

    operations = [
        migrations.RunPython(
            tag_post_grandfather_bonuses,
            migrations.RunPython.noop,
        ),
    ]
