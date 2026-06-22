"""Seed CoachChatMessage history from a participant's PRE-CHAT conversation:
their check-in comments (user messages) interleaved with that day's coach
suggestion notes (coach replies), in chronological order. So opening the chat
shows the real history that happened before the chat UI existed.

Idempotent: skips messages already present (matched on role+date+text), and
won't duplicate the morning notes already seeded by the check-in view.

Usage:
    python manage.py backfill_chat --participant 10
    python manage.py backfill_chat --all
    python manage.py backfill_chat --participant 10 --dry-run
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from daily.models import (
    CoachChatMessage, CoachSuggestion, DailyCheckIn, DailyParticipant,
)


class Command(BaseCommand):
    help = "Backfill coach chat history from comments + coach notes."

    def add_arguments(self, parser):
        parser.add_argument("--participant", type=int, default=None)
        parser.add_argument("--all", action="store_true", help="All participants with history.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        if opts["all"]:
            pids = list(
                DailyCheckIn.objects.values_list("participant_id", flat=True).distinct()
            )
        elif opts["participant"] is not None:
            pids = [opts["participant"]]
        else:
            self.stderr.write("Pass --participant ID or --all"); return

        total = 0
        for pid in pids:
            try:
                participant = DailyParticipant.objects.get(id=pid)
            except DailyParticipant.DoesNotExist:
                continue
            total += self._backfill_one(participant, opts["dry_run"])
        self.stdout.write(self.style.SUCCESS(f"Done. {total} messages backfilled."))

    def _backfill_one(self, participant, dry_run):
        # Collect every (timestamp, role, text, date, suggestion) event.
        events = []

        # User messages = check-in comments.
        for ci in DailyCheckIn.objects.filter(participant=participant).exclude(comment=""):
            ts = ci.updated_at or ci.submitted_at
            events.append((ts, CoachChatMessage.ROLE_USER, ci.comment.strip(), ci.date, None))

        # Coach replies = suggestion notes (not dismissed). Ordered by created_at.
        notes = (
            CoachSuggestion.objects.filter(
                check_in__participant=participant
            ).exclude(status=CoachSuggestion.STATUS_DISMISSED)
             .exclude(suggestion_text="")
             .select_related("check_in")
        )
        for s in notes:
            events.append((s.created_at, CoachChatMessage.ROLE_COACH,
                           s.suggestion_text.strip(), s.check_in.date, s))

        # Chronological order; stable on ties.
        events.sort(key=lambda e: (e[0] or timezone.now()))

        # Existing chat messages → dedup set (role, date, text).
        existing = set(
            (m.role, m.date, m.text.strip())
            for m in CoachChatMessage.objects.filter(participant=participant)
        )

        created = 0
        for ts, role, text, d, sugg in events:
            if not text:
                continue
            key = (role, d, text)
            if key in existing:
                continue
            existing.add(key)
            if dry_run:
                self.stdout.write(f"  [{participant.display_name}] {d} {role}: {text[:60]}")
                created += 1
                continue
            m = CoachChatMessage.objects.create(
                participant=participant, role=role, text=text, date=d,
                suggestion=sugg if role == CoachChatMessage.ROLE_COACH else None,
                notified=True,  # historical — don't re-notify the admin
            )
            # Backdate created_at to the real time so order + timestamps read true.
            CoachChatMessage.objects.filter(pk=m.pk).update(created_at=ts)
            created += 1

        self.stdout.write(f"{participant.display_name}: +{created} messages")
        return created
