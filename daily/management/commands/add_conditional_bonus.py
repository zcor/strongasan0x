"""Add a conditional bonus item to a participant's current checklist — a bonus
that only unlocks once a prerequisite item is checked done (Spencer's "8+ hours
unlocks after I check 7+ hours" idea).

Usage:
    python manage.py add_conditional_bonus --participant 10 \
        --key bonus_sleep_8h --label "Slept 8+ hours" --unlock-after q_sleep

Idempotent: skips if a bonus with that key already exists.
"""
from django.core.management.base import BaseCommand

from daily.models import DailyParticipant


class Command(BaseCommand):
    help = "Add a conditional (unlock-after) bonus item to a participant's checklist."

    def add_arguments(self, parser):
        parser.add_argument("--participant", type=int, required=True)
        parser.add_argument("--key", required=True, help="bonus_ prefixed slug")
        parser.add_argument("--label", required=True, help="≤60 chars, past tense")
        parser.add_argument("--unlock-after", required=True,
                            help="key of the item that must be done first")

    def handle(self, *args, **opts):
        try:
            participant = DailyParticipant.objects.get(id=opts["participant"])
        except DailyParticipant.DoesNotExist:
            self.stderr.write(f"No participant {opts['participant']}"); return

        version = participant.get_or_create_current_checklist()
        bonus = list(version.bonus_questions or [])
        core_keys = {q["key"] for q in version.questions}
        all_bonus_keys = {q["key"] for q in bonus}

        key = opts["key"]
        if not key.startswith("bonus_"):
            key = "bonus_" + key
        if key in all_bonus_keys:
            self.stdout.write(f"{participant.display_name}: '{key}' already exists — skipping.")
            return

        unlock = opts["unlock_after"]
        if unlock not in core_keys and unlock not in all_bonus_keys:
            self.stderr.write(
                f"unlock-after '{unlock}' is not a current item key. "
                f"Core keys: {sorted(core_keys)}"
            )
            return

        bonus.append({"key": key, "label": opts["label"][:60], "unlock_after": unlock})
        version.bonus_questions = bonus
        version.save(update_fields=["bonus_questions"])
        self.stdout.write(self.style.SUCCESS(
            f"Added '{opts['label']}' to {participant.display_name} "
            f"(unlocks after '{unlock}')."
        ))
