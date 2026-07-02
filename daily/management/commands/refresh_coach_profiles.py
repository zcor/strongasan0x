"""Refresh the distilled attestation-history CoachProfile for warriors.

The overnight coach reads a warrior's CoachProfile as its long-term memory of
who they are (their training pattern, trajectory, gaps, declared intent) — so
it coaches like it knows them instead of reasoning from 5 checkboxes alone. The
profile is normally kept fresh lazily inside the nightly coach run
(coach_runner.refresh_coach_profile, hash-guarded so it only pays for an LLM
call when attestations actually changed). This command is the on-demand /
backfill entry point: seed profiles for everyone at once, or force a rebuild.

Usage:
    python manage.py refresh_coach_profiles                 # all warriors, hash-guarded
    python manage.py refresh_coach_profiles --participant 10   # one person
    python manage.py refresh_coach_profiles --force           # rebuild even if unchanged
    python manage.py refresh_coach_profiles --dry-run         # report who HAS attestations

Fail-safe: one participant raising (bad data, model error) is logged and
skipped — it never breaks the run for everyone else.
"""
import logging

from django.core.management.base import BaseCommand

from daily.models import DailyParticipant
from daily.services.coach_runner import refresh_coach_profile

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Refresh each warrior's distilled attestation-history CoachProfile."

    def add_arguments(self, parser):
        parser.add_argument("--participant", type=int, default=None,
                            help="Only this participant id (testing).")
        parser.add_argument("--force", action="store_true",
                            help="Rebuild even if the attestation set is unchanged.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report who has attestation history; build nothing.")

    def handle(self, *args, **opts):
        # Only warriors bridged to a telegram identity can have attestations.
        qs = DailyParticipant.objects.filter(
            is_active=True, telegram_mapping__isnull=False
        )
        if opts["participant"] is not None:
            qs = DailyParticipant.objects.filter(id=opts["participant"])

        built = skipped = errored = 0
        for participant in qs:
            try:
                if opts["dry_run"]:
                    from rollcall.models import Attestation
                    n = Attestation.objects.filter(
                        telegram_user_id=participant.telegram_mapping_id,
                        is_hidden=False,
                    ).count()
                    verdict = f"{n} attestations" if n else "no attestations — skip"
                    self.stdout.write(f"[dry-run] {participant.display_name}: {verdict}")
                    continue

                if refresh_coach_profile(participant, force=opts["force"]):
                    built += 1
                    self.stdout.write(f"{participant.display_name}: profile refreshed")
                else:
                    skipped += 1
            except Exception as exc:  # never let one bad participant break the run
                errored += 1
                logger.exception("refresh_coach_profiles: failed for participant %s: %s",
                                 getattr(participant, "id", "?"), exc)

        if not opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS(
                f"Done. built={built} skipped(unchanged/none)={skipped} errored={errored}"
            ))
