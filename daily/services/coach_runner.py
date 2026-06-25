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

import logging
from datetime import date, timedelta

from .ai_coach import build_coach_context, generate_suggestion

logger = logging.getLogger(__name__)

RECENT_DAYS = 7  # how many days of history the coach reads as context


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
    since = check_in.date - timedelta(days=RECENT_DAYS)
    recent = list(
        DailyCheckIn.objects
        .filter(participant=participant, date__gte=since, date__lte=check_in.date)
        .select_related("checklist_version")
        .prefetch_related("answers")
        .order_by("-date")
    )

    context = build_coach_context(participant, check_in, recent)
    result = generate_suggestion(context, refinement=refinement or None)
    if result is None:
        return False
    suggestion_text, proposed_questions, proposed_bonus, model_name, cost_usd = result

    CoachSuggestion.objects.create(
        check_in=check_in,
        suggestion_text=suggestion_text,
        proposed_questions=proposed_questions,
        proposed_bonus=proposed_bonus,
        rationale=f"refinement: {refinement}" if refinement else "",
        status=CoachSuggestion.STATUS_PENDING,
        model_name=model_name,
        cost_usd=cost_usd,
    )
    return True


def coach_prior_day(participant, today: date) -> bool:
    """If the most recent check-in BEFORE `today` has no coach suggestion,
    run the coach for it (once). Only the most recent — we don't burn API
    calls coaching a backlog of missed days.

    Returns True if a coach run happened (caller may want to log).
    """
    from ..models import DailyCheckIn

    prior = (
        DailyCheckIn.objects
        .filter(participant=participant, date__lt=today)
        .order_by("-date")
        .first()
    )
    if prior is None or prior.suggestions.exists():
        return False
    return run_coach(prior.id)
