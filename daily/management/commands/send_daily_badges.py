"""Send each subscribed device a Web Push that refreshes the home-screen badge
to today's remaining to-do count. Run by cron — this is the only way to update
an iOS PWA badge while the app is closed.

Two modes:
  - default: push EVERY subscription right now (manual/testing, or a single
    fixed-time daily cron in a one-timezone world).
  - --hourly: the production mode. Run this every hour; it pushes a given
    device only when the local hour in THAT participant's own timezone has
    reached their personal TARGET hour, and only once per their local day.
    So a non-Pacific user's badge resets to the fresh count on THEIR morning,
    not the server's. (Fixes the 06:30-UTC bug where the badge reflected the
    prior, already-cleared day.)

    The target hour is "smart morning timing": each participant's typical
    wake hour, learned from their own activity history (see
    daily.services.tz.target_morning_hour), minus one — so the badge lands
    just BEFORE they wake instead of at a fixed 6am. Falls back to 6am local
    for participants without enough activity history to estimate a wake time.
    An admin-set participant.morning_target_hour overrides the learned value.

    Cross-midnight targets: a target hour of 20-23 (only reachable via the
    admin override) means "this push is FOR TOMORROW, fired the evening
    before" — e.g. target 23 for someone who wakes ~1am gets their next-day
    badge at 11pm. In that case the day being badged is local_today + 1: the
    count pushed is TOMORROW's full core count (nothing can be done yet, so
    it's simply the size of the current checklist's core list), and
    last_badge_date is stamped with the BADGED day (tomorrow) so the dedupe
    guarantee stays "one badge per badged local day per subscription" — it
    can't fire at 23:00 and again the next morning. Targets 0-19 keep the
    plain same-day behavior.

Usage:
    python manage.py send_daily_badges                    # push everyone now
    python manage.py send_daily_badges --hourly           # per-user-tz morning push
    python manage.py send_daily_badges --participant 11   # one person (testing)
    python manage.py send_daily_badges --dry-run          # compute, don't send

Fail-safe: a dead subscription (404/410 from the push service) is deleted; a
transient error bumps fail_count and the sub is pruned after too many. The job
never raises out of the loop, so one bad device can't break everyone's morning.
"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from daily.models import PushSubscription
from daily.services.tz import (
    participant_local_hour,
    participant_today,
    target_morning_hour,
)
from daily.views import remaining_core_today

logger = logging.getLogger(__name__)

MAX_FAILS = 5  # prune a subscription after this many consecutive failures
# A target hour at or past this is an EVENING push for the NEXT local day
# (admin-override territory — the learned estimate clamps to 0-11).
EVENING_TARGET_MIN = 20


class Command(BaseCommand):
    help = "Push today's remaining-to-do count to each subscribed device's badge."

    def add_arguments(self, parser):
        parser.add_argument("--participant", type=int, default=None,
                            help="Only this participant id (testing).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Compute counts and log, but send nothing.")
        parser.add_argument("--hourly", action="store_true",
                            help="Per-user-tz mode: push only at the morning hour "
                                 "in each participant's own timezone, once per local day.")

    def handle(self, *args, **opts):
        pub = getattr(settings, "VAPID_PUBLIC_KEY", "")
        priv = getattr(settings, "VAPID_PRIVATE_KEY", "")
        subject = getattr(settings, "VAPID_SUBJECT", "mailto:admin@strongasan0x.com")
        if not (pub and priv):
            self.stderr.write("VAPID keys not configured — nothing sent.")
            return

        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            self.stderr.write("pywebpush not installed — nothing sent.")
            return

        hourly = opts["hourly"]
        qs = PushSubscription.objects.select_related("participant").all()
        if opts["participant"] is not None:
            qs = qs.filter(participant_id=opts["participant"])

        sent = pruned = skipped = held = 0
        for sub in qs:
            # Each user's "today" is resolved in THEIR timezone (fixes the badge
            # reflecting the prior, already-cleared day for non-Pacific users).
            local_today = participant_today(sub.participant)
            # The local day this badge is FOR. Same as local_today except for
            # evening targets (below), where the push is for TOMORROW.
            badged_day = local_today

            if hourly:
                # Only this participant's own TARGET hour fires (admin override
                # if set, else learned wake hour minus one, else 6am local).
                # There is exactly ONE target hour per participant, so someone
                # with an evening override can never ALSO match a same-day
                # morning hour.
                target = target_morning_hour(sub.participant)
                if participant_local_hour(sub.participant) != target:
                    held += 1
                    continue
                if target >= EVENING_TARGET_MIN:
                    # Evening push FOR tomorrow (e.g. target 23 for a ~1am
                    # waker): badge and dedupe against the NEXT local day.
                    badged_day = local_today + timedelta(days=1)
                # Once per BADGED local day. For evening targets the stamp is
                # tomorrow's date, so the next hourly runs (which resolve the
                # same badged_day until the following evening) all hold.
                if sub.last_badge_date == badged_day:
                    held += 1
                    continue

            try:
                if badged_day == local_today:
                    count = remaining_core_today(sub.participant, local_today)
                else:
                    # Tomorrow: nothing can be done yet, so the badge is simply
                    # the full core size of the current checklist version.
                    count = len(sub.participant.get_or_create_current_checklist().questions)
            except Exception as exc:  # never let one bad participant break the run
                logger.exception("send_daily_badges: count failed for %s: %s", sub.participant_id, exc)
                skipped += 1
                continue

            if opts["dry_run"]:
                self.stdout.write(
                    f"[dry-run] {sub.participant.display_name}: badge={count} for {badged_day}"
                )
                continue

            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=json.dumps({"count": count, "day": badged_day.isoformat()}),
                    vapid_private_key=priv,
                    vapid_claims={"sub": subject},
                    timeout=10,
                )
                sub.last_pushed_at = timezone.now()
                sub.last_badge_date = badged_day  # mark the BADGED day done (hourly dedupe)
                sub.fail_count = 0
                sub.save(update_fields=["last_pushed_at", "last_badge_date", "fail_count"])
                sent += 1
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    # Subscription is gone for good — delete it.
                    sub.delete()
                    pruned += 1
                else:
                    sub.fail_count += 1
                    if sub.fail_count >= MAX_FAILS:
                        sub.delete()
                        pruned += 1
                    else:
                        sub.save(update_fields=["fail_count"])
                    logger.warning("send_daily_badges: push failed (%s) for sub %s: %s",
                                   status, sub.id, exc)
            except Exception as exc:
                logger.exception("send_daily_badges: unexpected error for sub %s: %s", sub.id, exc)
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. sent={sent} pruned={pruned} skipped={skipped} held={held}"
        ))
