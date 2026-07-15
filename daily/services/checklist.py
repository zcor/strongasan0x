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

# Upper bound on the user-curated core list. Generous (one item per gym station
# plus extras) but bounded so a runaway add-loop can't create an unusable page.
MAX_CHECKLIST_SIZE = 20


def dismiss_pending_mutations(participant: DailyParticipant) -> int:
    """Retire queued list rewrites when a participant enters support-only mode.

    This belongs to the mode transition, not the dashboard GET: executing the
    same no-op UPDATE on every page view adds a full database round trip.
    """
    return CoachSuggestion.objects.filter(
        check_in__participant=participant,
        proposed_questions__isnull=False,
        status=CoachSuggestion.STATUS_PENDING,
    ).update(
        status=CoachSuggestion.STATUS_DISMISSED,
        responded_at=timezone.now(),
    )


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
        .exclude(status__in=[
            CoachSuggestion.STATUS_DISMISSED,
            CoachSuggestion.STATUS_APPLIED,
            CoachSuggestion.STATUS_SHOWN,  # no-op already evaluated; don't re-process
        ])
        .select_related("check_in")
        .order_by("check_in__date", "created_at")
    )
    promoted = 0
    for suggestion in pending:
        current = participant.checklist_versions.filter(is_current=True).first()
        proposed = suggestion.proposed_questions
        # Beta users edit their list instantly (add/swap), so a proposal
        # generated last night may predate edits made after it: reconcile
        # first, or applying it wholesale would revert them. Legacy stays
        # frozen: proposals apply exactly as generated, as they always have.
        if participant.beta and current and suggestion.base_questions is not None:
            proposed = _reconcile_user_edits(
                suggestion.base_questions, current.questions, proposed
            )
        # The mutation engine proposes bare {key,label} questions; re-attach the
        # current version's sub-item lists to every preserved key BEFORE the
        # validity and no-op checks, or applying any suggestion would silently
        # wipe user-curated drawers (and an identical proposal would not
        # compare equal to a current question that has sub-items).
        proposed = _merge_subitems(
            current.questions if current else [], proposed
        )
        if not _is_valid_questions(proposed):
            logger.warning("daily.checklist: skipping invalid proposed_questions on suggestion %s", suggestion.id)
            suggestion.status = CoachSuggestion.STATUS_DISMISSED
            suggestion.responded_at = timezone.now()
            suggestion.save(update_fields=["status", "responded_at"])
            continue

        proposed_bonus = suggestion.proposed_bonus or None

        if (
            current
            and _questions_equal(current.questions, proposed)
            and _questions_equal(current.bonus_questions or [], proposed_bonus or [])
        ):
            # True no-op (core AND bonus unchanged) — don't churn versions,
            # and DON'T mark APPLIED. There's nothing to undo, so the note
            # should render as a plain coach note, not "checklist updated".
            suggestion.status = CoachSuggestion.STATUS_SHOWN
            suggestion.save(update_fields=["status"])
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
    # The user curates their own list (e.g. one item per gym station), bounded
    # at MAX_CHECKLIST_SIZE. Must be a non-empty list of unique {key,label}.
    # A habit may carry an optional `items` list of {key,label} sub-items (the
    # nested detail checklist); every key, sub-item keys included, is globally
    # unique so per-day answers never collide.
    if not isinstance(questions, list) or not (1 <= len(questions) <= MAX_CHECKLIST_SIZE):
        return False
    seen = set()

    def _add_kv(key, label) -> bool:
        if not isinstance(key, str) or not isinstance(label, str):
            return False
        if not key or not label or key in seen:
            return False
        seen.add(key)
        return True

    for q in questions:
        if not isinstance(q, dict):
            return False
        if not _add_kv(q.get("key"), q.get("label")):
            return False
        items = q.get("items")
        if items is not None:
            if not isinstance(items, list):
                return False
            for s in items:
                if not isinstance(s, dict) or not _add_kv(s.get("key"), s.get("label")):
                    return False
    return True


def _reconcile_user_edits(base, current_questions, proposed) -> list:
    """Overlay edits the user made AFTER a proposal was generated onto it.
    `base` is the core list the model saw; anything in the current list but
    not in base is a later user addition (carry it over, appended in current
    order); anything in base but no longer current was user-removed (drop it
    from the proposal too); and stable keys with new labels are user renames.
    The proposal's own key changes (a coach swap: same slot, new key) are
    untouched. Non-list proposals pass through for _is_valid_questions to
    reject. Trims coach-introduced items first if the overlay would exceed
    MAX_CHECKLIST_SIZE — user curation outranks coaching.
    """
    if not isinstance(proposed, list) or not isinstance(base, list):
        return proposed
    base_keys = {q.get("key") for q in base if isinstance(q, dict)}
    current_keys = {
        q.get("key") for q in current_questions if isinstance(q, dict)
    }
    removed = base_keys - current_keys
    result = [
        q for q in proposed
        if not (isinstance(q, dict) and q.get("key") in removed)
    ]
    # A stable key whose label changed is a user rename. Carry that wording
    # over the queued proposal so an overnight mutation generated before the
    # edit cannot silently restore the misspelling/old label.
    base_by_key = {
        q.get("key"): q for q in base
        if isinstance(q, dict) and q.get("key")
    }
    current_by_key = {
        q.get("key"): q for q in current_questions
        if isinstance(q, dict) and q.get("key")
    }
    renamed = {
        key: current_q.get("label")
        for key, current_q in current_by_key.items()
        if key in base_by_key
        and current_q.get("label") != base_by_key[key].get("label")
    }
    result = [
        {**q, "label": renamed[q.get("key")]}
        if isinstance(q, dict) and q.get("key") in renamed
        else q
        for q in result
    ]
    result_keys = {q.get("key") for q in result if isinstance(q, dict)}
    added = [
        {"key": q["key"], "label": q["label"]}
        for q in current_questions
        if isinstance(q, dict)
        and q.get("key") not in base_keys
        and q.get("key") not in result_keys
    ]
    result += added
    overflow = len(result) - MAX_CHECKLIST_SIZE
    if overflow > 0:
        fresh = [
            q for q in result
            if isinstance(q, dict)
            and q.get("key") not in base_keys and q.get("key") not in current_keys
        ]
        for q in reversed(fresh):
            if overflow <= 0:
                break
            result.remove(q)
            overflow -= 1
        logger.info("daily.checklist: reconciled proposal trimmed to cap")
    return result


def all_version_keys(version) -> set:
    """Every key in play on a version: core habits, their nested sub-items,
    and bonus items. Freshly minted keys must be globally unique so a
    sub-item's per-day answer can never collide with another item's."""
    keys = {q["key"] for q in version.questions}
    keys |= {q["key"] for q in (version.bonus_questions or [])}
    for q in version.questions:
        for s in (q.get("items") or []):
            keys.add(s["key"])
    return keys


def _merge_subitems(current_questions: List[dict], proposed) -> list:
    """Carry each current question's `items` sub-list onto the proposed
    question. A proposal that explicitly ships its own `items` for a key wins;
    one that omits them (the common engine output) inherits the existing
    drawer instead of erasing it.

    Match by KEY first, then fall back to (case-folded) LABEL: the overnight
    engine is allowed to mint a fresh key while keeping a habit's wording, and
    a key-only match would silently drop that habit's curated sub-items on
    every such rename. Label fallback reattaches them across the rename. Non-
    list input is returned as-is for _is_valid_questions to reject."""
    if not isinstance(proposed, list):
        return proposed
    by_key, by_label = {}, {}
    for q in current_questions:
        if isinstance(q, dict) and q.get("items"):
            if q.get("key"):
                by_key[q["key"]] = q["items"]
            if isinstance(q.get("label"), str):
                by_label[q["label"].strip().casefold()] = q["items"]
    merged = []
    for q in proposed:
        if isinstance(q, dict) and "items" not in q:
            items = by_key.get(q.get("key"))
            if items is None and isinstance(q.get("label"), str):
                items = by_label.get(q["label"].strip().casefold())
            if items is not None:
                q = dict(q, items=items)
        merged.append(q)
    return merged


def _sub_pairs(q: dict):
    return [(s.get("key"), s.get("label")) for s in (q.get("items") or [])]


def _questions_equal(a: List[dict], b: List[dict]) -> bool:
    if len(a) != len(b):
        return False
    return all(
        x.get("key") == y.get("key")
        and x.get("label") == y.get("label")
        and _sub_pairs(x) == _sub_pairs(y)
        for x, y in zip(a, b)
    )
