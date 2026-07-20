"""Re-run the legacy-bonus health tagging to catch stragglers.

0020_tag_legacy_bonus_health was a one-time backfill. Bonus items generated
AFTER it ran can still be untagged: generate_one_bonus only sets
category == "health" when health_only=True, and the default append path
(_generate_and_append_bonus, health_only=False) leaves it off. Those items
then vanish from the beta dashboard (health_bonus_items keeps only tagged
bonuses) and their taps 400.

Every existing bonus was produced by the health-only coach, so tagging any
still-untagged item "health" is factually correct — the same reasoning 0020
used. This pass simply catches what post-dated it (e.g. bonus_grip_test),
which matters once beta is enabled for everyone.
"""
from django.db import migrations


def tag_remaining_bonuses(apps, schema_editor):
    ChecklistVersion = apps.get_model("daily", "ChecklistVersion")
    for version in ChecklistVersion.objects.exclude(bonus_questions=None).iterator():
        changed = False
        for item in version.bonus_questions or []:
            if isinstance(item, dict) and "category" not in item:
                item["category"] = "health"
                changed = True
        if changed:
            version.save(update_fields=["bonus_questions"])


class Migration(migrations.Migration):

    dependencies = [
        ("daily", "0020_tag_legacy_bonus_health"),
    ]

    operations = [
        migrations.RunPython(tag_remaining_bonuses, migrations.RunPython.noop),
    ]
