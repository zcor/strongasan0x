"""
Daily checklist views — continuous-day model.

There is no submit. The day is always live:
  - checkin (GET): the one screen. Lazily coaches the most recent prior
    day on first visit of a new day, auto-applies pending mutations,
    renders core + bonus items with their states.
  - set_item_state (POST /daily/item/): tap=done / skip / back to pending.
  - save_comment (POST /daily/comment/): autosave the day's comment.
  - respond_to_suggestion: Undo an applied mutation from the morning note.
  - token_login / reset_to_baseline: unchanged.

Dev-only (DEBUG=True): ?as_of=YYYY-MM-DD overrides "today".
"""
import json
import logging
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .auth import (
    SESSION_DAILY_PARTICIPANT_ID,
    login_with_token,
    require_daily_actor,
    warrior_session_keys_set,
)
from .models import (
    BASELINE_QUESTIONS,
    ChecklistVersion,
    CoachSuggestion,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)
from .services.ai_coach import build_coach_context, generate_suggestion
from .services.checklist import apply_pending_mutations, revert_to_baseline

logger = logging.getLogger(__name__)

RECENT_DAYS = 7
BONUS_REVEAL_AT = 3  # bonus section appears once this many core items are done


def _resolve_today(request) -> date:
    real_today = timezone.localdate()
    if not settings.DEBUG:
        return real_today
    as_of_raw = request.GET.get("as_of")
    if not as_of_raw:
        return real_today
    try:
        return datetime.strptime(as_of_raw, "%Y-%m-%d").date()
    except ValueError:
        return real_today


def _as_of_query(request) -> str:
    if settings.DEBUG:
        val = request.GET.get("as_of")
        if val:
            return f"?as_of={val}"
    return ""


def token_login(request, token):
    if warrior_session_keys_set(request):
        return render(
            request,
            "daily/token_conflict.html",
            {"telegram_username": request.session.get("warrior_telegram_username", "")},
            status=409,
        )

    participant = login_with_token(request, token)
    if participant is None:
        return render(request, "daily/token_invalid.html", status=404)

    return redirect(f"/daily/checkin/{_as_of_query(request)}")


def ensure_prior_day_coached(participant: DailyParticipant, today: date) -> bool:
    """If the most recent check-in BEFORE today has no coach suggestion,
    run the coach for it synchronously (once). Only the most recent —
    we don't burn API calls coaching a backlog of missed days.

    Returns True if a coach run happened (caller may want to log).
    """
    prior = (
        DailyCheckIn.objects
        .filter(participant=participant, date__lt=today)
        .order_by("-date")
        .first()
    )
    if prior is None or prior.suggestions.exists():
        return False
    _run_coach(prior.id)
    return True


def _morning_note(participant: DailyParticipant, today: date):
    """Most recent non-dismissed suggestion from a prior day."""
    return (
        CoachSuggestion.objects.filter(
            check_in__participant=participant,
            check_in__date__lt=today,
        )
        .exclude(status=CoachSuggestion.STATUS_DISMISSED)
        .order_by("-created_at")
        .first()
    )


def _get_or_create_today(participant: DailyParticipant, today: date, version: ChecklistVersion) -> DailyCheckIn:
    check_in, _ = DailyCheckIn.objects.get_or_create(
        participant=participant,
        date=today,
        defaults={"checklist_version": version, "source": DailyCheckIn.SOURCE_WEB},
    )
    return check_in


@ensure_csrf_cookie  # page is AJAX-driven; force the csrftoken cookie even with no rendered form
@require_daily_actor
@require_http_methods(["GET"])
def checkin(request):
    participant: DailyParticipant = request.daily_participant
    today = _resolve_today(request)

    # 1. Coach the most recent prior day if it hasn't been (sync, ~3-5s,
    #    at most once — afterwards suggestions exist and this no-ops).
    coached = ensure_prior_day_coached(participant, today)
    if coached:
        logger.info("daily.checkin: lazily coached prior day for %s", participant)

    # 2. Apply any pending mutations so today reflects the coach's plan.
    applied = apply_pending_mutations(participant, as_of=today)
    if applied:
        logger.info("daily.checkin: applied %d pending mutations for %s", applied, participant)

    current_version = participant.get_or_create_current_checklist()
    existing = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    states = existing.answers_by_key() if existing else {}

    note = _morning_note(participant, today)
    if note and note.status == CoachSuggestion.STATUS_PENDING:
        note.status = CoachSuggestion.STATUS_SHOWN
        note.save(update_fields=["status"])

    core_items = [
        {"key": q["key"], "label": q["label"], "state": states.get(q["key"], "pending")}
        for q in current_version.questions
    ]
    done_count = sum(1 for i in core_items if i["state"] == "done")

    bonus_items = [
        {"key": q["key"], "label": q["label"], "state": states.get(q["key"], "pending")}
        for q in (current_version.bonus_questions or [])
    ]
    bonus_done = any(i["state"] != "pending" for i in bonus_items)

    context = {
        "participant": participant,
        "today": today,
        "note": note,
        "note_was_applied": note is not None and note.status == CoachSuggestion.STATUS_APPLIED,
        "core_items": core_items,
        "bonus_items": bonus_items,
        # Reveal bonus once threshold hit — or keep visible if any bonus
        # already has a state (don't hide items the user interacted with).
        "bonus_revealed": bool(bonus_items) and (done_count >= BONUS_REVEAL_AT or bonus_done),
        "bonus_reveal_at": BONUS_REVEAL_AT,
        "done_count": done_count,
        "comment": existing.comment if existing else "",
        "is_baseline": _is_baseline_questions(current_version.questions),
        "debug_as_of": request.GET.get("as_of") if settings.DEBUG else None,
        "tomorrow_qs": f"?as_of={(today + timedelta(days=1)).isoformat()}" if settings.DEBUG else "",
        "yesterday_qs": f"?as_of={(today - timedelta(days=1)).isoformat()}" if settings.DEBUG else "",
    }
    return render(request, "daily/checkin.html", context)


def _is_baseline_questions(qs):
    if len(qs) != len(BASELINE_QUESTIONS):
        return False
    return all(
        a.get("key") == b.get("key") and a.get("label") == b.get("label")
        for a, b in zip(qs, BASELINE_QUESTIONS)
    )


@require_daily_actor
@require_http_methods(["POST"])
def set_item_state(request):
    """Tap/skip an item. Body (form or JSON): key, state."""
    participant = request.daily_participant
    today = _resolve_today(request)

    if request.content_type == "application/json":
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"ok": False, "error": "bad_json"}, status=400)
        key = str(body.get("key", "")).strip()
        state = str(body.get("state", "")).strip()
    else:
        key = request.POST.get("key", "").strip()
        state = request.POST.get("state", "").strip()

    valid_states = {s for s, _ in DailyCheckInAnswer.STATE_CHOICES}
    if state not in valid_states:
        return JsonResponse({"ok": False, "error": "bad_state"}, status=400)

    version = request.daily_participant.get_or_create_current_checklist()
    valid_keys = set(version.question_keys()) | {
        q["key"] for q in (version.bonus_questions or [])
    }
    if key not in valid_keys:
        return JsonResponse({"ok": False, "error": "bad_key"}, status=400)

    with transaction.atomic():
        check_in = _get_or_create_today(participant, today, version)
        DailyCheckInAnswer.objects.update_or_create(
            check_in=check_in,
            question_key=key,
            defaults={"state": state},
        )

    check_in.refresh_from_db()
    states = check_in.answers_by_key()
    core_keys = version.question_keys()
    done_count = sum(1 for k in core_keys if states.get(k) == "done")
    return JsonResponse({
        "ok": True,
        "done_count": done_count,
        "bonus_revealed": bool(version.bonus_questions) and done_count >= BONUS_REVEAL_AT,
    })


@require_daily_actor
@require_http_methods(["POST"])
def save_comment(request):
    participant = request.daily_participant
    today = _resolve_today(request)
    comment = request.POST.get("comment", "")
    if request.content_type == "application/json":
        try:
            comment = json.loads(request.body).get("comment", "")
        except json.JSONDecodeError:
            return JsonResponse({"ok": False}, status=400)

    version = participant.get_or_create_current_checklist()
    check_in = _get_or_create_today(participant, today, version)
    check_in.comment = comment.strip()
    check_in.save(update_fields=["comment", "updated_at"])
    return JsonResponse({"ok": True})


@require_daily_actor
@require_http_methods(["POST"])
def respond_to_suggestion(request, suggestion_id):
    """Undo an applied mutation from the morning note."""
    participant = request.daily_participant
    suggestion = get_object_or_404(
        CoachSuggestion,
        id=suggestion_id,
        check_in__participant=participant,
    )
    if request.POST.get("response") == "dismissed":
        if suggestion.status == CoachSuggestion.STATUS_APPLIED and suggestion.applied_version:
            _revert_applied_suggestion(participant, suggestion)
        suggestion.status = CoachSuggestion.STATUS_DISMISSED
        suggestion.responded_at = timezone.now()
        suggestion.save(update_fields=["status", "responded_at"])
        messages.success(request, "Change undone — back to the previous checklist.")
    return redirect(f"/daily/checkin/{_as_of_query(request)}")


def _revert_applied_suggestion(participant, suggestion):
    applied_version = suggestion.applied_version
    with transaction.atomic():
        applied_version.is_current = False
        applied_version.save(update_fields=["is_current"])
        parent = applied_version.derived_from
        if parent and not parent.is_current:
            participant.checklist_versions.filter(is_current=True).update(is_current=False)
            parent.is_current = True
            parent.save(update_fields=["is_current"])
        elif not parent:
            revert_to_baseline(participant)


@require_daily_actor
@require_http_methods(["POST"])
def reset_to_baseline_view(request):
    participant = request.daily_participant
    revert_to_baseline(participant)
    messages.success(request, "Checklist reset to the original 5 questions.")
    return redirect(f"/daily/checkin/{_as_of_query(request)}")


def _run_coach(check_in_id: int, refinement: str = ""):
    """Synchronous coach run (mod_wsgi-safe — daemon threads are not)."""
    try:
        check_in = DailyCheckIn.objects.select_related(
            "participant", "checklist_version"
        ).get(id=check_in_id)
    except DailyCheckIn.DoesNotExist:
        logger.warning("daily._run_coach: check_in %s vanished", check_in_id)
        return

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
        return
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
