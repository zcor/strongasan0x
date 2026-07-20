"""Run the overnight coach for every active participant's most recent
un-coached day. This is the REAL overnight coach: cron runs it every HOUR so
the coach's "your list updates overnight" promise is true — chat requests the
user typed during the day get read and applied for the next day, without them
having to reopen the app first.

Safe to run hourly (confirmed): each call resolves `participant_today()` in
THAT participant's own timezone, and `coach_prior_day` only acts when the
most recent check-in before today has no suggestion yet. So of the 24 hourly
ticks a day, only the single tick right after a participant's local midnight
finds un-coached work; the other 23 are a no-op query per participant. Two
overlapping command runs can't double-coach — the "does a suggestion already
exist" check is the same guard either way. This hourly cadence is what lets
"smart morning timing" (see
daily.services.tz.target_morning_hour) push the badge as early as a
participant's local midnight: the coach job is scheduled to run just before
the badge job each hour (see deploy/crontab.ox) so that day's note and any
checklist mutation are ready before the badge push reads them.

Dashboard rendering never calls the model as a fallback: doing so delays the
first response by several seconds and leaves an installed PWA on an OS-owned
blank launch surface. The next hourly tick safely catches any missed boundary.
The separate morning-report request may generate a support-only report on
demand, outside the usable dashboard's critical path.

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
                    from daily.models import CoachSuggestion
                    prior = (
                        DailyCheckIn.objects
                        .filter(participant=participant, date__lt=today)
                        .order_by("-date")
                        .first()
                    )
                    # Mirror coach_prior_day: a user-authored evening plan
                    # alone doesn't count as coached (the note-around-the-plan
                    # run still happens).
                    if prior is None:
                        self.stdout.write(f"[dry-run] {participant.display_name}: no prior check-in — skip")
                    elif prior.suggestions.exclude(
                        rationale=CoachSuggestion.RATIONALE_EVENING_PLAN
                    ).exists():
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
