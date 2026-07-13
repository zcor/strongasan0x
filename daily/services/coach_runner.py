"""
Coach runner — the shared "run the overnight coach for a check-in" logic.

Two call sites use this:
  - the lazy in-request path (daily/views.py): when a user opens the app on a
    new day, coach their most recent un-coached prior day synchronously.
  - the scheduled path (management/commands/run_coach_for_all.py): a nightly
    cron coaches every active participant's most recent un-coached day, so
    chat requests actually apply overnight instead of only on next open.

Both go through `run_coach` (one check-in) and `coach_prior_day` (find the
most recent un-coached prior day for a participant and coach it). Keeping the
logic here — not in views — means the cron doesn't import the request layer
and there's exactly one coach implementation to reason about.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta

from .ai_coach import build_coach_context, distill_profile, generate_suggestion

logger = logging.getLogger(__name__)

RECENT_DAYS = 7  # how many days of history the coach reads as context


def refresh_coach_profile(participant, force: bool = False) -> bool:
    """Ensure `participant` has an up-to-date distilled CoachProfile from their
    Roll Call attestation history — the coach's long-term memory.

    Hash-guarded: reads the warrior's attestations, hashes the set, and only
    calls the (paid) distillation model when the set has CHANGED since the last
    build (or force=True). So calling this before every nightly coach run costs
    an extra LLM call ONLY when there's genuinely new attestation material —
    the common case (nothing new) is a couple of cheap queries and a no-op.

    Fail-safe: never raises. Returns True iff the profile was (re)built with a
    live model call. A participant with no telegram identity / no attestations
    gets no profile and the coach falls back to its no-profile behavior.
    """
    from ..models import CoachProfile

    try:
        mapping_id = getattr(participant, "telegram_mapping_id", None)
        if not mapping_id:
            return False  # external / no attestations → no profile

        from rollcall.models import Attestation

        # Oldest-to-newest so the distilled trajectory reads chronologically.
        atts = list(
            Attestation.objects
            .filter(telegram_user_id=mapping_id, is_hidden=False)
            .order_by("posted_at")
            .values_list("id", "raw_text", "updated_at")
        )
        if not atts:
            return False

        # Hash the identity+mtime of the set: any add, edit, or delete flips it.
        digest = hashlib.sha256(
            "|".join(f"{aid}:{updated.isoformat()}" for aid, _, updated in atts).encode()
        ).hexdigest()

        existing = CoachProfile.objects.filter(participant=participant).first()
        if existing and existing.source_hash == digest and not force:
            return False  # unchanged → no LLM call

        attestation_text = "\n\n---\n\n".join(rt for _, rt, _ in atts if (rt or "").strip())
        if not attestation_text.strip():
            return False

        result = distill_profile(participant.display_name, attestation_text)
        if result is None:
            return False  # model/parse failure → keep any existing profile
        profile_text, model_name, cost_usd = result

        CoachProfile.objects.update_or_create(
            participant=participant,
            defaults={
                "profile_text": profile_text,
                "source_hash": digest,
                "attestation_count": len(atts),
                "model_name": model_name,
                "cost_usd": cost_usd,
            },
        )
        logger.info(
            "daily.coach_runner: refreshed profile for %s (%d attestations, $%s)",
            participant.display_name, len(atts), cost_usd,
        )
        return True
    except Exception as exc:  # never let profile refresh break a coach run
        logger.exception("daily.coach_runner.refresh_coach_profile failed: %s", exc)
        return False


def run_coach(check_in_id: int, refinement: str = "") -> bool:
    """Run the coach for one check-in: build context from recent days, call
    the model, and save a CoachSuggestion. Synchronous (mod_wsgi-safe —
    daemon threads are not). Returns True if a suggestion was saved.

    Never raises for a model/parse failure — generate_suggestion returns None
    and we no-op. (A genuinely missing check-in is logged and skipped.)
    """
    # Imported here (not at module load) to avoid a circular import: models →
    # nothing here, but keeping the ORM import local mirrors the rest of the
    # service layer and keeps this module import-light for the command.
    from ..models import CoachSuggestion, DailyCheckIn

    try:
        check_in = DailyCheckIn.objects.select_related(
            "participant", "checklist_version"
        ).get(id=check_in_id)
    except DailyCheckIn.DoesNotExist:
        logger.warning("daily.coach_runner: check_in %s vanished", check_in_id)
        return False

    participant = check_in.participant
    # Beta support-only Jamie (mutations off) never runs the overnight
    # list-rewriting engine. Gating here covers every caller at once:
    # wrap_day, the nightly cron, and the lazy morning catch-up.
    if participant.beta and not participant.ai_mutations_enabled:
        logger.info(
            "daily.coach_runner: %s is beta support-only; skipping coach run",
            participant.display_name,
        )
        return False
    # Refresh the warrior's distilled attestation profile before building
    # context (hash-guarded — a real LLM call only when attestations changed).
    refresh_coach_profile(participant)
    since = check_in.date - timedelta(days=RECENT_DAYS)
    recent = list(
        DailyCheckIn.objects
        .filter(participant=participant, date__gte=since, date__lte=check_in.date)
        .select_related("checklist_version")
        .prefetch_related("answers")
        .order_by("-date")
    )

    # A USER-AUTHORED evening plan ("Plan tomorrow" chat flow) on this check-in
    # takes precedence over anything the coach would invent: the coach writes
    # its morning note AROUND the plan and must not propose a competing list.
    plan = _pending_evening_plan(check_in)
    plan_items = list(plan.proposed_questions) if plan else None

    context = build_coach_context(participant, check_in, recent)
    result = generate_suggestion(
        context, refinement=refinement or None, evening_plan=plan_items,
    )
    if result is None:
        return False
    suggestion_text, proposed_questions, proposed_bonus, model_name, cost_usd = result

    rationale = f"refinement: {refinement}" if refinement else ""
    if plan_items:
        # Code-enforce the user's plan — never trust the model to have copied
        # it verbatim. The plan suggestion applies first (older created_at);
        # this note then no-ops on core (same list), so no double mutation —
        # it exists to be the supportive morning note + optional fresh bonus.
        proposed_questions = plan_items
        rationale = CoachSuggestion.RATIONALE_EVENING_PLAN_NOTE

    CoachSuggestion.objects.create(
        check_in=check_in,
        suggestion_text=suggestion_text,
        proposed_questions=proposed_questions,
        proposed_bonus=proposed_bonus,
        # What the model was shown — the reconcile base for user edits made
        # between generation and tomorrow's apply (see apply_pending_mutations).
        base_questions=[
            {"key": q["key"], "label": q["label"]}
            for q in context["current_questions"]
        ],
        rationale=rationale,
        status=CoachSuggestion.STATUS_PENDING,
        model_name=model_name,
        cost_usd=cost_usd,
    )
    return True


def _pending_evening_plan(check_in):
    """The still-pending user-authored evening plan on `check_in`, or None.
    (Once auto-apply promotes it its status is APPLIED and this returns None —
    so a re-run after the morning can't re-anchor on a stale plan.)"""
    from ..models import CoachSuggestion

    return (
        check_in.suggestions
        .filter(
            rationale=CoachSuggestion.RATIONALE_EVENING_PLAN,
            status=CoachSuggestion.STATUS_PENDING,
            proposed_questions__isnull=False,
        )
        .order_by("-created_at")
        .first()
    )


def coach_prior_day(participant, today: date) -> bool:
    """If the most recent check-in BEFORE `today` has no coach suggestion,
    run the coach for it (once). Only the most recent — we don't burn API
    calls coaching a backlog of missed days.

    A USER-AUTHORED evening plan alone doesn't count as "already coached":
    the coach still runs once to write the morning note AROUND the plan
    (run_coach detects the pending plan, keeps the user's list, and stamps
    the note RATIONALE_EVENING_PLAN_NOTE — which DOES count afterwards, so
    this can't loop).

    Returns True if a coach run happened (caller may want to log).
    """
    from ..models import CoachSuggestion, DailyCheckIn

    prior = (
        DailyCheckIn.objects
        .filter(participant=participant, date__lt=today)
        .order_by("-date")
        .first()
    )
    if prior is None or prior.suggestions.exclude(
        rationale=CoachSuggestion.RATIONALE_EVENING_PLAN
    ).exists():
        return False
    return run_coach(prior.id)
