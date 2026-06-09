"""
Checklist version management: auto-apply pending AI mutations,
revert to baseline, and resolve the active version for a given date.
"""
from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import List

from django.db import transaction
from django.utils import timezone

from ..models import (
    BASELINE_QUESTIONS,
    ChecklistVersion,
    CoachSuggestion,
    DailyParticipant,
)

logger = logging.getLogger(__name__)


def apply_pending_mutations(participant: DailyParticipant, as_of: date_cls) -> int:
    """Apply any AI mutations whose source check-in is BEFORE `as_of` and
    whose suggestion wasn't dismissed.

    Returns the number of versions promoted (usually 0 or 1, but the
    loop handles backlog when the participant skipped days).
    """
    pending = (
        CoachSuggestion.objects
        .filter(check_in__participant=participant, check_in__date__lt=as_of)
        .filter(proposed_questions__isnull=False)
        .exclude(status__in=[CoachSuggestion.STATUS_DISMISSED, CoachSuggestion.STATUS_APPLIED])
        .select_related("check_in")
        .order_by("check_in__date", "created_at")
    )
    promoted = 0
    for suggestion in pending:
        proposed = suggestion.proposed_questions
        if not _is_valid_questions(proposed):
            logger.warning("daily.checklist: skipping invalid proposed_questions on suggestion %s", suggestion.id)
            suggestion.status = CoachSuggestion.STATUS_DISMISSED
            suggestion.responded_at = timezone.now()
            suggestion.save(update_fields=["status", "responded_at"])
            continue

        proposed_bonus = suggestion.proposed_bonus or None

        current = participant.checklist_versions.filter(is_current=True).first()
        if (
            current
            and _questions_equal(current.questions, proposed)
            and _questions_equal(current.bonus_questions or [], proposed_bonus or [])
        ):
            # True no-op (core AND bonus unchanged) — don't churn versions.
            suggestion.status = CoachSuggestion.STATUS_APPLIED
            suggestion.applied_at = timezone.now()
            suggestion.applied_version = current
            suggestion.save(update_fields=["status", "applied_at", "applied_version"])
            continue

        with transaction.atomic():
            if current:
                current.is_current = False
                current.save(update_fields=["is_current"])
            new_version = ChecklistVersion.objects.create(
                participant=participant,
                questions=proposed,
                bonus_questions=proposed_bonus,
                source=ChecklistVersion.SOURCE_AI_MUTATION,
                derived_from=current,
                is_current=True,
            )
            suggestion.status = CoachSuggestion.STATUS_APPLIED
            suggestion.applied_at = timezone.now()
            suggestion.applied_version = new_version
            suggestion.save(update_fields=["status", "applied_at", "applied_version"])
        promoted += 1
    return promoted


def revert_to_baseline(participant: DailyParticipant) -> ChecklistVersion:
    """Swap the current checklist back to the Stronger-in-60 baseline."""
    current = participant.checklist_versions.filter(is_current=True).first()
    if current and _questions_equal(current.questions, BASELINE_QUESTIONS):
        return current
    with transaction.atomic():
        if current:
            current.is_current = False
            current.save(update_fields=["is_current"])
        return ChecklistVersion.objects.create(
            participant=participant,
            questions=list(BASELINE_QUESTIONS),
            source=ChecklistVersion.SOURCE_USER_RESET,
            derived_from=current,
            is_current=True,
        )


def _is_valid_questions(questions) -> bool:
    if not isinstance(questions, list) or len(questions) != 5:
        return False
    seen = set()
    for q in questions:
        if not isinstance(q, dict):
            return False
        key = q.get("key")
        label = q.get("label")
        if not isinstance(key, str) or not isinstance(label, str):
            return False
        if not key or not label or key in seen:
            return False
        seen.add(key)
    return True


def _questions_equal(a: List[dict], b: List[dict]) -> bool:
    if len(a) != len(b):
        return False
    return all(
        x.get("key") == y.get("key") and x.get("label") == y.get("label")
        for x, y in zip(a, b)
    )
