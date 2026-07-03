"""Send an opted-in device a gentle Web Push at that participant's chosen
local hour inviting them to plan tomorrow's 3 items via the "Plan tomorrow"
chat flow ("What's tomorrow's frog?").

OPT-IN ONLY: the nudge fires ONLY for participants whose
DailyParticipant.evening_nudge_hour override is SET. A null override means
"no evening nudge for this person" — there is NO blanket default hour. (An
earlier design defaulted everyone to 10pm local, but for the night-owl users
we dogfood with, 10pm is early afternoon — a fixed default is wrong. Opt-in
lets us roll out per user.)

This is a SEPARATE job from send_daily_badges — same robustness pattern
(hourly, per-participant-tz, once-per-local-day, dead-subscription pruning,
per-sub exception isolation), but its own dedup field
(PushSubscription.last_evening_nudge_date) so neither job can suppress the
other. send_daily_badges owns last_badge_date; this command never touches it.

Skip logic: if the participant has ALREADY planned tomorrow — a pending
CoachSuggestion with rationale=RATIONALE_EVENING_PLAN on TODAY's check-in
(the same row the "Plan tomorrow" chat flow writes to; see
daily/services/coach_runner.py:_pending_evening_plan and
daily/views.py:wrap_day) — the nudge is skipped. Nudging someone who already
did the thing the nudge asks for is just noise.

Usage:
    python manage.py send_evening_plan_nudge                  # push every OPTED-IN user now
    python manage.py send_evening_plan_nudge --hourly         # per-user-tz evening push (cron mode)
    python manage.py send_evening_plan_nudge --participant 11 # one person (testing)
    python manage.py send_evening_plan_nudge --dry-run        # compute, don't send

Fail-safe: identical to send_daily_badges — a dead subscription (404/410) is
deleted; a transient error bumps fail_count and the sub is pruned after too
many. The job never raises out of the loop.
"""
import json
import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from daily.models import CoachSuggestion, DailyCheckIn, PushSubscription
from daily.services.tz import participant_local_hour, participant_today

logger = logging.getLogger(__name__)

MAX_FAILS = 5  # prune a subscription after this many consecutive failures

NUDGE_TITLE = "Plan tomorrow \U0001f305"
NUDGE_BODY = (
    "What's the one thing you've been putting off? "
    "Set tomorrow's 3 and wake up ready to win."
)


def evening_nudge_hour(participant):
    """The local HOUR (0-23) at which this participant's evening 'Plan
    tomorrow' nudge should fire, or None if they have NOT opted in.

    Opt-in is expressed by setting DailyParticipant.evening_nudge_hour; a null
    override means "no evening nudge for this person" — there is no default
    hour. No learned/cross-midnight variant like morning_target_hour: this is
    a fixed, user-chosen reminder time, not a wake-time prediction."""
    return getattr(participant, "evening_nudge_hour", None)


def already_planned_tomorrow(participant, today) -> bool:
    """True if the participant has a pending user-authored evening plan on
    TODAY's check-in — i.e. they've already done what this nudge is asking
    ("Plan tomorrow" chat flow; see coach_runner._pending_evening_plan and
    views.wrap_day, which use the identical filter). No check-in yet today
    means nothing has been planned, so this is False (never crashes on a
    missing row)."""
    check_in = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    if check_in is None:
        return False
    return check_in.suggestions.filter(
        rationale=CoachSuggestion.RATIONALE_EVENING_PLAN,
        status=CoachSuggestion.STATUS_PENDING,
        proposed_questions__isnull=False,
    ).exists()


class Command(BaseCommand):
    help = "Push a gentle evening reminder to plan tomorrow's 3 items."

    def add_arguments(self, parser):
        parser.add_argument("--participant", type=int, default=None,
                            help="Only this participant id (testing).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Compute who would be nudged and log, but send nothing.")
        parser.add_argument("--hourly", action="store_true",
                            help="Per-user-tz mode: push only at the evening hour "
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

        sent = pruned = skipped = held = already_planned = opted_out = 0
        for sub in qs:
            # OPT-IN GATE (both modes): no override set = no evening nudge for
            # this person. Null means "off", not "nudge at some default hour".
            target = evening_nudge_hour(sub.participant)
            if target is None:
                opted_out += 1
                continue

            # Each user's "today" is resolved in THEIR timezone, same as the
            # badge job — this is the local day the "Plan tomorrow" flow
            # writes its suggestion onto.
            local_today = participant_today(sub.participant)

            if hourly:
                if participant_local_hour(sub.participant) != target:
                    held += 1
                    continue
                # Once per local day. Dedicated field — see model docstring:
                # send_daily_badges owns last_badge_date, this command owns
                # last_evening_nudge_date, so the two jobs can never collide.
                if sub.last_evening_nudge_date == local_today:
                    held += 1
                    continue

            try:
                if already_planned_tomorrow(sub.participant, local_today):
                    already_planned += 1
                    continue
            except Exception as exc:  # never let one bad participant break the run
                logger.exception(
                    "send_evening_plan_nudge: planned-check failed for %s: %s",
                    sub.participant_id, exc,
                )
                skipped += 1
                continue

            if opts["dry_run"]:
                self.stdout.write(
                    f"[dry-run] {sub.participant.display_name}: would nudge for {local_today}"
                )
                continue

            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=json.dumps({
                        "kind": "evening_nudge",
                        "title": NUDGE_TITLE,
                        "body": NUDGE_BODY,
                        "day": local_today.isoformat(),
                    }),
                    vapid_private_key=priv,
                    vapid_claims={"sub": subject},
                    timeout=10,
                )
                sub.last_pushed_at = timezone.now()
                sub.last_evening_nudge_date = local_today  # dedicated dedupe field
                sub.fail_count = 0
                sub.save(update_fields=["last_pushed_at", "last_evening_nudge_date", "fail_count"])
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
                    logger.warning("send_evening_plan_nudge: push failed (%s) for sub %s: %s",
                                   status, sub.id, exc)
            except Exception as exc:
                logger.exception("send_evening_plan_nudge: unexpected error for sub %s: %s", sub.id, exc)
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. sent={sent} pruned={pruned} skipped={skipped} held={held} "
            f"already_planned={already_planned} opted_out={opted_out}"
        ))
