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
    # Prod-safe backfill: ?day=YYYY-MM-DD views/edits a PAST day (max 7
    # back, never future). Lets users log evening items (screens-off,
    # magnesium) the next morning via the week strip.
    day_raw = request.GET.get("day")
    if day_raw:
        try:
            d = datetime.strptime(day_raw, "%Y-%m-%d").date()
            if real_today - timedelta(days=7) <= d <= real_today:
                return d
        except ValueError:
            pass
        return real_today
    if not settings.DEBUG:
        return real_today
    as_of_raw = request.GET.get("as_of")
    if not as_of_raw:
        return real_today
    try:
        return datetime.strptime(as_of_raw, "%Y-%m-%d").date()
    except ValueError:
        return real_today


def _is_backfill(request) -> bool:
    """True when viewing a past day via ?day= (no coaching/bonus there)."""
    return _resolve_today(request) < timezone.localdate() and bool(request.GET.get("day"))


def _as_of_query(request) -> str:
    day = request.GET.get("day")
    if day and _is_backfill(request):
        return f"?day={day}"
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
    """Most recent non-dismissed suggestion from a prior day.

    The seeded welcome note (rationale='seed_welcome') is one-time: once
    the participant has any genuine coach suggestion OR has completed any
    item, the welcome retires so it doesn't greet them as "first time"
    every day.
    """
    qs = (
        CoachSuggestion.objects.filter(
            check_in__participant=participant,
            check_in__date__lt=today,
        )
        .exclude(status=CoachSuggestion.STATUS_DISMISSED)
        .order_by("-created_at")
    )
    note = qs.first()
    if note is not None and note.rationale == "seed_welcome":
        has_real_note = qs.exclude(rationale="seed_welcome").exists()
        has_activity = DailyCheckInAnswer.objects.filter(
            check_in__participant=participant, state="done"
        ).exists()
        if has_real_note or has_activity:
            # Welcome has served its purpose — show a real note if one
            # exists, otherwise nothing.
            return qs.exclude(rationale="seed_welcome").first()
    return note


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
    backfill = _is_backfill(request)

    if not backfill:
        # 1. Coach the most recent prior day if it hasn't been (sync,
        #    ~3-5s, at most once — afterwards suggestions exist, no-op).
        coached = ensure_prior_day_coached(participant, today)
        if coached:
            logger.info("daily.checkin: lazily coached prior day for %s", participant)

        # 2. Apply pending mutations so today reflects the coach's plan.
        applied = apply_pending_mutations(participant, as_of=today)
        if applied:
            logger.info("daily.checkin: applied %d pending mutations for %s", applied, participant)

    current_version = participant.get_or_create_current_checklist()
    existing = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    states = existing.answers_by_key() if existing else {}

    # A past day renders with the checklist it actually had that day.
    render_version = existing.checklist_version if (backfill and existing) else current_version

    note = None
    note_core_changed = False
    if not backfill:
        note = _morning_note(participant, today)
        if note and note.status == CoachSuggestion.STATUS_PENDING:
            note.status = CoachSuggestion.STATUS_SHOWN
            note.save(update_fields=["status"])
        # "checklist updated" + Undo only when the CORE 5 actually changed
        # (bonus-only version churn reads as a plain note).
        if note and note.status == CoachSuggestion.STATUS_APPLIED and note.applied_version_id:
            parent = note.applied_version.derived_from
            note_core_changed = parent is None or parent.questions != note.applied_version.questions

    core_items = [
        {"key": q["key"], "label": q["label"], "state": states.get(q["key"], "pending")}
        for q in render_version.questions
    ]
    done_count = sum(1 for i in core_items if i["state"] == "done")

    bonus_items = [
        {"key": q["key"], "label": q["label"], "state": states.get(q["key"], "pending")}
        for q in (render_version.bonus_questions or [])
    ]
    bonus_done = any(i["state"] != "pending" for i in bonus_items)

    # First-run intro: shown until the participant has marked anything, ever.
    first_visit = not DailyCheckInAnswer.objects.filter(
        check_in__participant=participant
    ).exclude(state=DailyCheckInAnswer.STATE_PENDING).exists()

    # Last 7 days (oldest → today) for the week strip. Each day scored
    # against the checklist version that was active THAT day.
    week = []
    by_date = {
        ci.date: ci for ci in DailyCheckIn.objects.filter(
            participant=participant,
            date__gte=today - timedelta(days=6),
            date__lte=today,
        ).select_related("checklist_version").prefetch_related("answers")
    }
    MINI_C = 62.8  # 2πr for the strip's r=10 mini-rings
    real_today = timezone.localdate()
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        ci = by_date.get(d)
        total = len(ci.checklist_version.questions) if ci else 5
        done = ci.score if ci else 0
        week.append({
            "label": d.strftime("%a")[0],
            "done": done,
            "total": total,
            "offset": round(MINI_C * (1 - done / total), 1) if total else MINI_C,
            "is_today": d == today,
            # Tap a past day to backfill it; today links back to the plain page.
            "href": "/daily/checkin/" if d == real_today else f"/daily/checkin/?day={d.isoformat()}",
        })

    context = {
        "participant": participant,
        "today": today,
        "backfill": backfill,
        "first_visit": first_visit and not backfill,
        "week": week,
        "note": note,
        "note_was_applied": note_core_changed,
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

    backfill = _is_backfill(request)
    existing_ci = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    if backfill and existing_ci:
        # Past days validate against the checklist they actually had.
        version = existing_ci.checklist_version
    else:
        version = request.daily_participant.get_or_create_current_checklist()
    valid_keys = set(version.question_keys()) | {
        q["key"] for q in (version.bonus_questions or [])
    }
    if key not in valid_keys:
        return JsonResponse({"ok": False, "error": "bad_key"}, status=400)

    bonus_keys = {q["key"] for q in (version.bonus_questions or [])}
    is_bonus = key in bonus_keys

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

    # Live bonus drip: generate a fresh bonus item when the user
    #  (a) just completed a BONUS item (refill the pile),
    #  (b) just SKIPPED a bonus (rejection = "deal me another"; the
    #      rejected one feeds the prompt as a negative example), or
    #  (c) just reached the core threshold with no bonus yet (first one).
    new_bonus = None
    bonus_resolved = is_bonus and state in (
        DailyCheckInAnswer.STATE_DONE, DailyCheckInAnswer.STATE_SKIP
    )
    crossed_threshold = (
        not is_bonus
        and state == DailyCheckInAnswer.STATE_DONE
        and done_count >= BONUS_REVEAL_AT
        and not version.bonus_questions
    )
    if (bonus_resolved or crossed_threshold) and not backfill:
        new_bonus = _generate_and_append_bonus(participant, version, check_in, states)

    version.refresh_from_db()
    return JsonResponse({
        "ok": True,
        "done_count": done_count,
        "bonus_revealed": bool(version.bonus_questions) and done_count >= BONUS_REVEAL_AT,
        "new_bonus": new_bonus,  # {key,label} to slide in, or null
    })


def _generate_and_append_bonus(participant, version, check_in, states):
    """Generate one fresh bonus item (live AI), append to the current
    version's bonus_questions, return {key,label} or None."""
    from rollcall.models import Attestation

    existing = list(version.questions) + list(version.bonus_questions or [])
    label_by_key = {q["key"]: q["label"] for q in existing}
    done_labels = [label_by_key[k] for k, s in states.items() if s == "done" and k in label_by_key]
    bonus_keys = {q["key"] for q in (version.bonus_questions or [])}
    rejected_labels = [
        label_by_key[k] for k, s in states.items()
        if s == "skip" and k in bonus_keys
    ]

    att_text = ""
    if version.participant.telegram_mapping_id:
        atts = Attestation.objects.filter(
            telegram_user_id=version.participant.telegram_mapping_id
        ).order_by("-posted_at")[:3]
        att_text = "\n\n".join(a.raw_text for a in atts)

    from .services.ai_coach import generate_one_bonus
    item = generate_one_bonus(
        participant_name=participant.display_name,
        attestation_text=att_text,
        existing_items=existing,
        today_done_labels=done_labels,
        today_comment=check_in.comment or "",
        rejected_labels=rejected_labels,
    )
    if item is None:
        return None
    with transaction.atomic():
        v = ChecklistVersion.objects.select_for_update().get(id=version.id)
        bonus = list(v.bonus_questions or [])
        if any(b["key"] == item["key"] for b in bonus):
            return None  # collision guard
        bonus.append(item)
        v.bonus_questions = bonus
        v.save(update_fields=["bonus_questions"])
    return item


@require_daily_actor
@require_http_methods(["POST"])
def next_bonus(request):
    """Bootstrap/refill endpoint: generate one bonus if the user has
    earned it (>=BONUS_REVEAL_AT core done today) and has no pending
    (uncompleted) bonus item. Used by the page on load for users who
    crossed the threshold before the live-drip feature existed, and as
    a safety net if a refill call was lost."""
    participant = request.daily_participant
    today = _resolve_today(request)
    if _is_backfill(request):
        return JsonResponse({"ok": True, "new_bonus": None})
    version = participant.get_or_create_current_checklist()
    check_in = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    if check_in is None:
        return JsonResponse({"ok": True, "new_bonus": None})

    states = check_in.answers_by_key()
    core_done = sum(1 for k in version.question_keys() if states.get(k) == "done")
    if core_done < BONUS_REVEAL_AT:
        return JsonResponse({"ok": True, "new_bonus": None})

    # Only top up if every existing bonus is resolved (done/skip) — keep
    # exactly one open bonus at a time so the pile grows by completion,
    # not by polling.
    open_bonus = [
        q for q in (version.bonus_questions or [])
        if states.get(q["key"], "pending") == "pending"
    ]
    if open_bonus:
        return JsonResponse({"ok": True, "new_bonus": None})

    item = _generate_and_append_bonus(participant, version, check_in, states)
    return JsonResponse({"ok": True, "new_bonus": item})


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
