"""
Flip the Climb `beta` flag (and optionally `ai_mutations_enabled`) on a
DailyParticipant by display name. Small, allowlist-safe management command so
beta testers can be recruited via ox-run without shell access.

    python manage.py set_beta --name Amy               # beta on, support-only
    python manage.py set_beta --name Amy --ai          # beta on + AI curation
    python manage.py set_beta --name Amy --off         # beta off
    python manage.py set_beta --name Amy --dry-run     # report only

Matches display_name case-insensitively and refuses to act when the name is
ambiguous (more than one match), so it can never silently flag the wrong user.
"""
from django.core.management.base import BaseCommand, CommandError

from daily.models import DailyParticipant
from daily.services.checklist import dismiss_pending_mutations


class Command(BaseCommand):
    help = "Set the Climb beta flag on a DailyParticipant by display name."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True,
                            help="Participant display_name (case-insensitive).")
        parser.add_argument("--off", action="store_true",
                            help="Turn beta OFF (default is ON).")
        parser.add_argument("--ai", action="store_true",
                            help="Also enable ai_mutations_enabled (overnight list curation).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change without saving.")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        matches = list(DailyParticipant.objects.filter(display_name__iexact=name))
        if not matches:
            raise CommandError(f"No DailyParticipant with display_name '{name}'.")
        if len(matches) > 1:
            ids = ", ".join(str(p.id) for p in matches)
            raise CommandError(
                f"Ambiguous: {len(matches)} participants named '{name}' (ids: {ids}). "
                "Refusing to guess."
            )

        p = matches[0]
        want_beta = not opts["off"]
        want_ai = bool(opts["ai"]) and want_beta  # AI only meaningful inside beta

        self.stdout.write(
            f"{p.display_name} (id={p.id}): "
            f"beta {p.beta} -> {want_beta}, "
            f"ai_mutations_enabled {p.ai_mutations_enabled} -> {want_ai}"
        )
        if opts["dry_run"]:
            self.stdout.write("dry-run: no changes saved.")
            return

        p.beta = want_beta
        p.ai_mutations_enabled = want_ai
        p.save(update_fields=["beta", "ai_mutations_enabled", "updated_at"])
        if not want_ai:
            dismiss_pending_mutations(p)
        self.stdout.write(self.style.SUCCESS("saved."))
