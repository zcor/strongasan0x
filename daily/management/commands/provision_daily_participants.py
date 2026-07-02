"""Pre-build Daily participants for every warrior who already has attestation
history, so when they DM the bot `/start` the bot just hands back a token that
points at an already-personalized app — no work happens at DM time.

For each TelegramUserMapping with ≥1 attestation:
  - get-or-create the DailyParticipant (linked to the mapping),
  - stamp source='telegram' + source_detail='@handle (tg <id>)',
  - stamp onboarded_at=now so the app SKIPS the naked-user questionnaire
    (they have real context — the coach seeds from their logs instead),
  - ensure an active DailyAccessToken (reused if present — never rotate a link
    we may already have shared).

With --coach, also tailor the checklist for participants who are still on the
bare baseline: derive a personalized stretch item from their attestations and
seal a welcome note (the same shape daily/seed_demo.py uses for the curated
demo warriors). Default is stamp-and-token only (no AI cost, no churn of the
already-curated live notes).

Usage:
    python manage.py provision_daily_participants            # stamp + token
    python manage.py provision_daily_participants --coach    # + AI tailoring
    python manage.py provision_daily_participants --dry-run  # report only

Idempotent: re-running only fills gaps (missing stamp/token, or — with --coach —
a still-baseline checklist). It never rotates tokens or overwrites a checklist
that's already been personalized.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from daily.models import (
    BASELINE_QUESTIONS,
    ChecklistVersion,
    CoachSuggestion,
    DailyAccessToken,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)
from daily.services.ai_coach import derive_stretch_item
from rollcall.models import Attestation, TelegramUserMapping

logger = logging.getLogger(__name__)

SOURCE_TELEGRAM = "telegram"


class Command(BaseCommand):
    help = "Pre-build Daily participants (+ tokens) for warriors with attestation history."

    def add_arguments(self, parser):
        parser.add_argument("--coach", action="store_true",
                            help="Also AI-tailor still-baseline checklists (costs a DeepSeek call each).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would happen; change nothing.")
        parser.add_argument("--mapping", type=int, default=None,
                            help="Only this TelegramUserMapping id (testing).")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]

        # FIRST, protect every EXISTING engaged user (warrior OR external) from
        # the naked-user onboarding. Onboarding replaces a participant's current
        # checklist with a generic seeded set — for Amy that wipes her live list,
        # for a warrior it throws away the attestation-tailored checklist. So any
        # participant who already has activity must be stamped onboarded_at, even
        # if they have no Telegram attestations (the warrior loop below misses
        # externals like Amy). Onboarding then shows ONLY to brand-new accounts.
        if opts["mapping"] is None:  # the targeted --mapping run skips this global pass
            self._protect_existing_users(dry)

        mappings = (
            TelegramUserMapping.objects
            .filter(attestations__isnull=False, is_active=True)
            .distinct()
        )
        if opts["mapping"] is not None:
            mappings = mappings.filter(id=opts["mapping"])

        provisioned = tokened = coached = skipped = 0
        for mapping in mappings:
            name = (mapping.linked_name or mapping.telegram_first_name
                    or mapping.telegram_username or f"tg_{mapping.telegram_user_id}")
            handle = f"@{mapping.telegram_username}" if mapping.telegram_username else f"tg {mapping.telegram_user_id}"
            detail = f"{handle} (tg {mapping.telegram_user_id})"

            try:
                participant, created = DailyParticipant.objects.get_or_create(
                    telegram_mapping=mapping,
                    defaults={
                        "display_name": name,
                        "kind": DailyParticipant.KIND_WARRIOR,
                        "source": SOURCE_TELEGRAM,
                        "source_detail": detail,
                        "onboarded_at": timezone.now(),
                    },
                )

                # Backfill stamps on a participant that pre-dates these fields.
                fields = []
                if not participant.source:
                    participant.source = SOURCE_TELEGRAM
                    participant.source_detail = detail
                    fields += ["source", "source_detail"]
                if participant.onboarded_at is None:
                    participant.onboarded_at = timezone.now()
                    fields.append("onboarded_at")
                if fields and not dry:
                    participant.save(update_fields=fields + ["updated_at"])

                if created or fields:
                    provisioned += 1

                # Ensure an active token (reuse — never rotate a shared link).
                token = participant.access_tokens.filter(revoked_at__isnull=True).first()
                if token is None:
                    if dry:
                        self.stdout.write(f"[dry-run] {name}: WOULD mint token")
                    else:
                        token = DailyAccessToken.objects.create(participant=participant)
                    tokened += 1

                if opts["coach"]:
                    if self._maybe_tailor(participant, mapping, name, dry):
                        coached += 1

                link = f"https://strongasan0x.com/daily/c/{token.token}/" if token else "(token pending)"
                self.stdout.write(f"  {name}: {link}")
            except Exception as exc:  # one bad mapping can't break the batch
                skipped += 1
                logger.exception("provision_daily_participants: failed for mapping %s: %s",
                                 getattr(mapping, "id", "?"), exc)

        verb = "[dry-run] would " if dry else ""
        self.stdout.write(self.style.SUCCESS(
            f"Done. {verb}provisioned={provisioned} tokened={tokened} "
            f"coached={coached} skipped={skipped}"
        ))

    # An existing user counts as "established" (→ protect from onboarding) once
    # they've genuinely engaged. Answered-item count is the clean discriminator:
    # it separates real users (Amy 22, CurveCap 45, Spencer 121, Battman 49) from
    # barely-there accounts (a lone empty check-in) that are better off going
    # through onboarding / fresh tailoring than being frozen half-set-up.
    PROTECT_MIN_ANSWERED = 10

    def _protect_existing_users(self, dry) -> int:
        """Stamp onboarded_at on every ESTABLISHED participant (warrior OR
        external) so the naked-user onboarding never wipes a live checklist.
        Engagement = answered ≥ PROTECT_MIN_ANSWERED items. Idempotent: only
        touches participants whose onboarded_at is still NULL."""
        from django.db.models import Count, Q
        from daily.models import DailyCheckInAnswer

        candidates = (
            DailyParticipant.objects
            .filter(onboarded_at__isnull=True)
            .annotate(answered=Count(
                "checkins__answers",
                filter=~Q(checkins__answers__state=DailyCheckInAnswer.STATE_PENDING),
            ))
            .filter(answered__gte=self.PROTECT_MIN_ANSWERED)
        )
        n = 0
        for p in candidates:
            n += 1
            if dry:
                self.stdout.write(f"[dry-run] PROTECT {p.display_name} "
                                  f"({p.answered} answered) → would set onboarded_at")
            else:
                p.onboarded_at = timezone.now()
                if not p.source:
                    p.source = "existing"
                p.save(update_fields=["onboarded_at", "source", "updated_at"])
                self.stdout.write(f"  PROTECT {p.display_name} ({p.answered} answered) → onboarded_at set")
        if not n:
            self.stdout.write("  (no unprotected established users — all good)")
        return n

    def _maybe_tailor(self, participant, mapping, name, dry) -> bool:
        """If the participant is still on the bare baseline, derive a stretch
        item from their attestations and seal a welcome note. Returns True if a
        tailoring happened (or would, in dry-run). Skips a checklist that's
        already been personalized (don't churn a live, curated note)."""
        current = participant.checklist_versions.filter(is_current=True).first()
        if current is not None and not _is_baseline(current.questions):
            return False  # already personalized — leave it

        atts = list(Attestation.objects.filter(telegram_user=mapping).order_by("-posted_at")[:4])
        if not atts:
            return False
        att_text = "\n\n---\n\n".join(f"[{a.posted_at.date()}]\n{a.raw_text}" for a in atts)

        if dry:
            self.stdout.write(f"[dry-run] {name}: WOULD AI-tailor checklist from {len(atts)} attestations")
            return True

        # Two baseline anchors + one personalized stretch = the core THREE.
        base = list(BASELINE_QUESTIONS[:2])
        stretch = derive_stretch_item(name, att_text, existing_items=base) \
            or {"key": "q_mobility", "label": "Did 10 minutes of mobility"}
        questions = base + [stretch]

        # Promote a fresh personalized version (demote any current one first).
        participant.checklist_versions.filter(is_current=True).update(is_current=False)
        version = ChecklistVersion.objects.create(
            participant=participant, questions=questions,
            source=ChecklistVersion.SOURCE_BASELINE, is_current=True,
        )
        # Seal a one-time welcome note as a prior-day suggestion so it greets
        # them on first open (same mechanism as seed_demo / _morning_note).
        note, cost = _welcome_note(name, questions, att_text, stretch)
        yesterday = timezone.localdate() - timedelta(days=1)
        ci, _ = DailyCheckIn.objects.get_or_create(
            participant=participant, date=yesterday,
            defaults={"checklist_version": version, "source": DailyCheckIn.SOURCE_WEB},
        )
        DailyCheckInAnswer.objects.bulk_create([
            DailyCheckInAnswer(check_in=ci, question_key=q["key"],
                               state=DailyCheckInAnswer.STATE_PENDING)
            for q in questions
        ], ignore_conflicts=True)
        CoachSuggestion.objects.create(
            check_in=ci, suggestion_text=note, proposed_questions=None,
            rationale="seed_welcome", status=CoachSuggestion.STATUS_PENDING,
            model_name="deepseek-chat", cost_usd=cost,
        )
        return True


def _is_baseline(questions) -> bool:
    """True if `questions` matches the original Stronger-in-60 baseline."""
    if len(questions) != len(BASELINE_QUESTIONS):
        return False
    return all(
        a.get("key") == b.get("key") and a.get("label") == b.get("label")
        for a, b in zip(questions, BASELINE_QUESTIONS)
    )


def _welcome_note(name, questions, att_text, stretch):
    """Generate a 2-3 sentence first-open welcome grounded in the user's logs.
    Returns (text, cost). Falls back to a generic note if AI is unavailable."""
    fallback = (f"Welcome, {name}. These three are your starting point — tap each "
                f"as you go, and tell the coach anything you want changed.")
    api_key = getattr(settings, "DEEPSEEK_API_KEY", "") or ""
    if not api_key:
        return fallback, None
    try:
        import openai
    except ImportError:
        return fallback, None
    qlist = "\n".join(f"  - {q['label']}" for q in questions)
    wp = (
        f"You are Coach Jamie. {name} is opening their personalized daily "
        f"checklist for the first time. Their 3 items are:\n{qlist}\n\n"
        f"The last item — \"{stretch['label']}\" — is a coach-picked stretch to "
        f"improve their health. Write a 2-3 sentence welcome referencing SPECIFIC "
        f"real details from their logs below and briefly say why you added "
        f"\"{stretch['label']}\". Cite only things literally in the logs; never "
        f"fabricate numbers. Output prose only."
    )
    try:
        client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": wp},
                {"role": "user", "content": f"{name}'s logs:\n\n{att_text[:3500]}"},
            ],
            max_tokens=240, temperature=0.4,
        )
    except Exception as exc:
        logger.warning("provision: welcome note generation failed: %s", exc)
        return fallback, None
    u = getattr(resp, "usage", None)
    cost = None
    if u:
        cost = Decimal(str((u.prompt_tokens / 1e6) * 0.14 + (u.completion_tokens / 1e6) * 0.28)).quantize(Decimal("0.000001"))
    text = (resp.choices[0].message.content or "").strip() or fallback
    return text, cost
