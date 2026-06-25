"""Run the overnight coach for every active participant's most recent
un-coached day. This is the REAL overnight coach: a nightly cron runs it so
the coach's "your list updates overnight" promise is true — chat requests the
user typed during the day get read and applied for the next day, without them
having to reopen the app first.

The lazy in-request path (daily/views.ensure_prior_day_coached) still exists as
a belt-and-suspenders for users whose day boundary the cron just missed; both
go through the same coach_runner, and both are idempotent (a day that already
has a suggestion is skipped), so running both never double-coaches.

Usage:
    python manage.py run_coach_for_all                 # all active participants
    python manage.py run_coach_for_all --participant 10   # one person (testing)
    python manage.py run_coach_for_all --dry-run          # report, coach nobody

Fail-safe: one participant raising (bad data, model error) is logged and
skipped — it can never break the run for everyone else.
"""
import logging

from django.core.management.base import BaseCommand

from daily.models import DailyCheckIn, DailyParticipant
from daily.services.coach_runner import coach_prior_day
from daily.services.tz import participant_today

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the overnight coach for each active participant's most recent un-coached day."

    def add_arguments(self, parser):
        parser.add_argument("--participant", type=int, default=None,
                            help="Only this participant id (testing).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report who WOULD be coached; coach nobody.")

    def handle(self, *args, **opts):
        qs = DailyParticipant.objects.filter(is_active=True)
        if opts["participant"] is not None:
            qs = qs.filter(id=opts["participant"])

        coached = skipped = errored = 0
        for participant in qs:
            try:
                # Resolve "today" in the PARTICIPANT'S timezone so we coach the
                # day that just ended for THEM, not the server's day.
                today = participant_today(participant)

                if opts["dry_run"]:
                    prior = (
                        DailyCheckIn.objects
                        .filter(participant=participant, date__lt=today)
                        .order_by("-date")
                        .first()
                    )
                    if prior is None:
                        self.stdout.write(f"[dry-run] {participant.display_name}: no prior check-in — skip")
                    elif prior.suggestions.exists():
                        self.stdout.write(f"[dry-run] {participant.display_name}: {prior.date} already coached — skip")
                    else:
                        self.stdout.write(f"[dry-run] {participant.display_name}: WOULD coach {prior.date}")
                    continue

                if coach_prior_day(participant, today):
                    coached += 1
                    logger.info("run_coach_for_all: coached prior day for %s", participant)
                else:
                    skipped += 1
            except Exception as exc:  # never let one bad participant break the run
                errored += 1
                logger.exception("run_coach_for_all: failed for participant %s: %s",
                                 getattr(participant, "id", "?"), exc)

        if not opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS(
                f"Done. coached={coached} skipped={skipped} errored={errored}"
            ))
