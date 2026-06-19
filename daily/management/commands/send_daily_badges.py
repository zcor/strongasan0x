"""Send each subscribed device a Web Push that refreshes the home-screen badge
to today's remaining to-do count. Run by a morning cron — this is the only way
to update an iOS PWA badge while the app is closed.

Usage:
    python manage.py send_daily_badges            # all active subscriptions
    python manage.py send_daily_badges --participant 11   # one person (testing)
    python manage.py send_daily_badges --dry-run          # compute, don't send

Fail-safe: a dead subscription (404/410 from the push service) is deleted; a
transient error bumps fail_count and the sub is pruned after too many. The job
never raises out of the loop, so one bad device can't break everyone's morning.
"""
import json
import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from daily.models import PushSubscription
from daily.views import remaining_core_today

logger = logging.getLogger(__name__)

MAX_FAILS = 5  # prune a subscription after this many consecutive failures


class Command(BaseCommand):
    help = "Push today's remaining-to-do count to each subscribed device's badge."

    def add_arguments(self, parser):
        parser.add_argument("--participant", type=int, default=None,
                            help="Only this participant id (testing).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Compute counts and log, but send nothing.")

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

        today = timezone.localdate()
        qs = PushSubscription.objects.select_related("participant").all()
        if opts["participant"] is not None:
            qs = qs.filter(participant_id=opts["participant"])

        sent = pruned = skipped = 0
        for sub in qs:
            try:
                count = remaining_core_today(sub.participant, today)
            except Exception as exc:  # never let one bad participant break the run
                logger.exception("send_daily_badges: count failed for %s: %s", sub.participant_id, exc)
                skipped += 1
                continue

            if opts["dry_run"]:
                self.stdout.write(f"[dry-run] {sub.participant.display_name}: badge={count}")
                continue

            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=json.dumps({"count": count}),
                    vapid_private_key=priv,
                    vapid_claims={"sub": subject},
                    timeout=10,
                )
                sub.last_pushed_at = timezone.now()
                sub.fail_count = 0
                sub.save(update_fields=["last_pushed_at", "fail_count"])
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
            f"Done. sent={sent} pruned={pruned} skipped={skipped}"
        ))
