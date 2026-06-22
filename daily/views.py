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
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .auth import (
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


# The streak bar SELF-CALIBRATES to each user (persona principle): it should
# require roughly what THAT person reliably does, so it stays meaningful for a
# power user (Spencer does ~5/day → his streak demands real effort) without
# punishing a beginner (Amy just shows up → showing up IS her streak).
#
# Floor = 1 so the lowest-engagement users keep a streak simply by using the
# app daily. Cap = 5 so the bar never exceeds a full core sweep.
STREAK_FLOOR = 1
STREAK_CAP = 5


def _day_done_count(ci: DailyCheckIn) -> int:
    """Items done that day, counting BOTH core and bonus."""
    return sum(1 for a in ci.answers.all() if a.state == DailyCheckInAnswer.STATE_DONE)


def _streak_bar(done_counts: list) -> int:
    """The per-user threshold: what this person reliably hits on a normal day.
    Use the MEDIAN of their recent daily done-counts (robust to one big/zero
    day), clamped to [FLOOR, CAP]. A user who routinely does 5 gets a bar of 5;
    one who routinely does 1-2 gets a bar of 1-2."""
    if not done_counts:
        return STREAK_FLOOR
    s = sorted(done_counts)
    median = s[len(s) // 2]
    return max(STREAK_FLOOR, min(STREAK_CAP, median))


def _current_streak(participant: DailyParticipant, today: date) -> int:
    """Consecutive days (back from today, or yesterday if today isn't done yet)
    where the user hit THEIR OWN bar. An unfinished today does NOT break a
    streak that was alive yesterday."""
    recent = {
        ci.date: ci
        for ci in DailyCheckIn.objects.filter(
            participant=participant, date__lte=today, date__gte=today - timedelta(days=400)
        ).select_related("checklist_version").prefetch_related("answers")
    }
    if not recent:
        return 0

    # Calibrate the bar from the user's recent ENGAGED days (last ~21 with any
    # activity) — so a string of zeros before they joined doesn't drag it down.
    recent_counts = [
        _day_done_count(ci)
        for d, ci in sorted(recent.items(), reverse=True)
        if _day_done_count(ci) > 0
    ][:21]
    bar = _streak_bar(recent_counts)

    streak = 0
    d = today
    today_ci = recent.get(today)
    if not (today_ci and _day_done_count(today_ci) >= bar):
        d = today - timedelta(days=1)   # don't punish an in-progress today
    while True:
        ci = recent.get(d)
        if ci is not None and _day_done_count(ci) >= bar:
            streak += 1
            d -= timedelta(days=1)
        else:
            break
    return streak


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
    # ?intro=1 force-shows it on demand (preview the onboarding anytime).
    first_visit = not DailyCheckInAnswer.objects.filter(
        check_in__participant=participant
    ).exclude(state=DailyCheckInAnswer.STATE_PENDING).exists()
    if request.GET.get("intro") == "1":
        first_visit = True
    # Intro REPLAY: bump INTRO_VERSION to re-show the "how this works" card once
    # to EVERY user (even veterans with history) — e.g. to gather feedback on a
    # change. The client shows it if its stored version is older, then records
    # the new version. Render the card whenever it's a genuine first visit OR a
    # replay is possible; JS makes the final show/hide call by localStorage.
    intro_replayable = (not first_visit) and (not backfill)

    # Wrapped: today already has a sealed coach reflection (via the
    # "Wrap up my day" button).
    wrapped = existing is not None and existing.suggestions.exclude(
        status=CoachSuggestion.STATUS_DISMISSED
    ).exists()

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
        "intro_replayable": intro_replayable,
        "intro_version": 1,  # bump to re-show the intro to everyone once
        "metric_fields": _metric_fields(participant, today) if not backfill else [],
        "wrapped": wrapped,
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
        "streak": _current_streak(participant, today) if not backfill else 0,
        "vapid_public_key": getattr(settings, "VAPID_PUBLIC_KEY", ""),
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

    # Live drip triggers (never in backfill):
    #  (a) completed a BONUS → refill the pile with a fresh one
    #  (b) SWAPPED any item (state=skip = "not interested") → generate a
    #      replacement in place; core swaps keep the ring completable
    #  (c) reached the core threshold with no bonus yet → first bonus
    new_bonus = None
    replacement = None
    if not backfill:
        if state == DailyCheckInAnswer.STATE_SKIP:
            replacement = _replace_item(participant, version, check_in, states, rejected_key=key, is_core=not is_bonus)
        elif is_bonus and state == DailyCheckInAnswer.STATE_DONE:
            new_bonus = _generate_and_append_bonus(participant, version, check_in, states)
        elif (
            not is_bonus
            and state == DailyCheckInAnswer.STATE_DONE
            and done_count >= BONUS_REVEAL_AT
            and not version.bonus_questions
        ):
            new_bonus = _generate_and_append_bonus(participant, version, check_in, states)

    version.refresh_from_db()
    core_keys = version.question_keys()
    done_count = sum(1 for k in core_keys if states.get(k) == "done")
    return JsonResponse({
        "ok": True,
        "done_count": done_count,
        "bonus_revealed": bool(version.bonus_questions) and done_count >= BONUS_REVEAL_AT,
        "new_bonus": new_bonus,            # {key,label} to append, or null
        "replacement": replacement,        # {key,label,core} swapped in place, or null
        "replaced_key": key if replacement else None,
    })


def _replace_item(participant, version, check_in, states, rejected_key, is_core):
    """User tapped "swap" — generate a grounded replacement and substitute
    it in the current version (core slot or bonus pile). Returns
    {key,label,core} or None (row stays struck-through as a fallback)."""
    from rollcall.models import Attestation
    from .services.ai_coach import generate_one_bonus

    existing = list(version.questions) + list(version.bonus_questions or [])
    label_by_key = {q["key"]: q["label"] for q in existing}
    done_labels = [label_by_key[k] for k, s in states.items() if s == "done" and k in label_by_key]
    rejected_labels = [
        label_by_key[k] for k, s in states.items()
        if s == "skip" and k in label_by_key
    ]

    att_text = ""
    if participant.telegram_mapping_id:
        atts = Attestation.objects.filter(
            telegram_user_id=participant.telegram_mapping_id
        ).order_by("-posted_at")[:3]
        att_text = "\n\n".join(a.raw_text for a in atts)

    item = generate_one_bonus(
        participant_name=participant.display_name,
        attestation_text=att_text,
        existing_items=existing,
        today_done_labels=done_labels,
        today_comment=check_in.comment or "",
        rejected_labels=rejected_labels,
        core=is_core,
    )
    if item is None:
        return None
    with transaction.atomic():
        v = ChecklistVersion.objects.select_for_update().get(id=version.id)
        all_keys = {q["key"] for q in v.questions} | {q["key"] for q in (v.bonus_questions or [])}
        if item["key"] in all_keys:
            return None  # collision guard
        if is_core:
            v.questions = [item if q["key"] == rejected_key else q for q in v.questions]
            v.save(update_fields=["questions"])
        else:
            bonus = [b for b in (v.bonus_questions or []) if b["key"] != rejected_key]
            bonus.append(item)
            v.bonus_questions = bonus
            v.save(update_fields=["bonus_questions"])
    return {**item, "core": is_core}


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
def wrap_day(request):
    """Amy's button: explicitly close out the day. Runs the coach on
    TODAY's check-in right now (instead of lazily tomorrow morning) and
    seals the morning note. Re-pressing after more taps re-wraps:
    unapplied suggestions are dismissed and the coach re-reads the day.
    Entirely optional — users who never press it get the lazy morning
    coach as before."""
    participant = request.daily_participant
    today = _resolve_today(request)
    if _is_backfill(request):
        return JsonResponse({"ok": False, "error": "no_wrap_in_backfill"}, status=400)

    version = participant.get_or_create_current_checklist()
    check_in = _get_or_create_today(participant, today, version)

    # Re-wrap: clear any prior un-applied reflection for today.
    check_in.suggestions.exclude(
        status__in=[CoachSuggestion.STATUS_APPLIED, CoachSuggestion.STATUS_DISMISSED]
    ).update(status=CoachSuggestion.STATUS_DISMISSED, responded_at=timezone.now())

    _run_coach(check_in.id)
    ready = check_in.suggestions.exclude(
        status=CoachSuggestion.STATUS_DISMISSED
    ).exists()
    return JsonResponse({"ok": True, "note_ready": ready})


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


# --- PWA (installable home-screen app) -------------------------------------
# The check-in page is installable to the home screen so it lives in the
# user's daily phone ritual. These two endpoints are public (no participant
# session required): the browser fetches them outside the auth flow. The
# installed app's start_url is /daily/checkin/, which the user's existing
# daily session authenticates — no token in the manifest.

def manifest(request):
    """Web app manifest. Served from /daily/ so its scope covers the app."""
    data = {
        "name": "Strong as an 0x — Daily",
        "short_name": "Daily",
        "description": "Your daily check-in. Fill the ring.",
        "start_url": "/daily/checkin/",
        "scope": "/daily/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1a1a2e",
        "theme_color": "#1a1a2e",
        "icons": [
            {"src": static("daily/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("daily/icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": static("daily/icons/icon-512-maskable.png"), "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }
    # application/manifest+json is the spec type; some iOS versions are picky,
    # but this is correct and Chrome requires it.
    return JsonResponse(data, content_type="application/manifest+json")


def service_worker(request):
    """Minimal network-first service worker.

    Served at /daily/sw.js so its scope covers /daily/. This is a LIVE app
    (the checklist mutates daily), so we deliberately do NOT cache content —
    the SW exists to make the app installable and to keep navigations fresh.
    A tiny offline fallback keeps the icon from opening to a dead page with
    no signal.
    """
    js = """\
const OFFLINE_MSG = 'You\\'re offline — reconnect to check in.';
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;            // never intercept POSTs (taps/saves)
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() =>
        new Response(
          '<!doctype html><meta name=viewport content="width=device-width">' +
          '<body style="font-family:-apple-system;background:#1a1a2e;color:#e8eaed;' +
          'display:flex;align-items:center;justify-content:center;height:100vh;' +
          'margin:0;text-align:center;padding:24px">' + OFFLINE_MSG + '</body>',
          { headers: { 'Content-Type': 'text/html' } }
        )
      )
    );
  }
  // Static assets (icons): cache-bust-free passthrough, fall back to cache nothing.
});

// --- Web Push: the morning badge ---
// The server pushes {count: N} each morning. We set the home-screen badge to
// N (today's remaining to-dos) WITHOUT opening the app. iOS requires us to
// also show a notification on each push, so we show a quiet one whose body
// just states the count — it doubles as the morning nudge.
self.addEventListener('push', (event) => {
  let count = 0;
  try { count = (event.data && event.data.json().count) || 0; } catch (e) {}
  event.waitUntil((async () => {
    try {
      if (navigator.setAppBadge) {
        if (count > 0) await navigator.setAppBadge(count); else await navigator.clearAppBadge();
      }
    } catch (e) {}
    // iOS will not deliver a silent push reliably — a visible notification is
    // required. Keep it minimal and on-message (it IS the daily counter).
    const title = count > 0 ? (count + ' to-do' + (count === 1 ? '' : 's') + ' today') : 'All done for today';
    const body = count > 0 ? 'Open Daily and fill your rings.' : 'Nice work — see you tomorrow.';
    await self.registration.showNotification(title, {
      body: body, badge: '/static/daily/icons/icon-192.png',
      icon: '/static/daily/icons/icon-192.png', tag: 'daily-badge', renotify: false,
    });
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow('/daily/checkin/'));
});
"""
    resp = HttpResponse(js, content_type="application/javascript")
    # SW must be served with a non-caching header during iteration so updates
    # ship; the browser re-checks the SW byte-for-byte on each navigation.
    resp["Cache-Control"] = "no-cache"
    # A service worker can only control a scope at or below its own path; an
    # explicit header lets us keep the file under /daily/ while scoping it there.
    resp["Service-Worker-Allowed"] = "/daily/"
    return resp


# --- Web Push: morning badge -----------------------------------------------

def remaining_core_today(participant: DailyParticipant, today: date) -> int:
    """How many CORE items the participant still has to do today (0..5). This
    is the number the home-screen badge shows. A day with no check-in yet =
    the full core count of their current checklist."""
    version = participant.get_or_create_current_checklist()
    total = len(version.questions)
    ci = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    if ci is None:
        return total
    done = ci.score  # done among CORE
    return max(0, total - done)


def _metric_fields(participant, today):
    """Build the metric quick-entry fields for the participant's active metrics,
    prefilled with today's readings. Returns [] when the participant has no
    metrics (Amy) → the template renders nothing. Each field is a flat dict the
    template can loop over; has_am_pm metrics yield two fields (am, pm)."""
    from .models import DailyMetric, DailyMetricReading
    metrics = list(DailyMetric.objects.filter(participant=participant, is_active=True))
    if not metrics:
        return []
    readings = {
        (r.metric_id, r.slot): r.value
        for r in DailyMetricReading.objects.filter(metric__in=metrics, date=today)
    }
    fields = []
    for m in metrics:
        slots = [("am", " (AM)"), ("pm", " (PM)")] if m.has_am_pm else [("", "")]
        for slot, suffix in slots:
            val = readings.get((m.id, slot))
            fields.append({
                "key": m.key, "slot": slot,
                "label": m.label + suffix, "unit": m.unit, "kind": m.kind,
                "value": ("" if val is None else (f"{val:.2f}".rstrip("0").rstrip("."))),
            })
    return fields


@require_daily_actor
@require_http_methods(["POST"])
def save_metric(request):
    """Upsert one metric reading. Body (JSON): key, slot, value (or "" to clear)."""
    from .models import DailyMetric, DailyMetricReading
    participant = request.daily_participant
    today = _resolve_today(request)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)
    key = str(body.get("key", "")).strip()
    slot = str(body.get("slot", "")).strip()
    raw = str(body.get("value", "")).strip()
    try:
        metric = DailyMetric.objects.get(participant=participant, key=key, is_active=True)
    except DailyMetric.DoesNotExist:
        return JsonResponse({"ok": False, "error": "unknown_metric"}, status=400)
    if slot not in (DailyMetricReading.SLOT_NONE, DailyMetricReading.SLOT_AM, DailyMetricReading.SLOT_PM):
        return JsonResponse({"ok": False, "error": "bad_slot"}, status=400)
    if raw == "":
        DailyMetricReading.objects.filter(metric=metric, date=today, slot=slot).delete()
        return JsonResponse({"ok": True, "cleared": True})
    try:
        from decimal import Decimal, InvalidOperation
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return JsonResponse({"ok": False, "error": "bad_value"}, status=400)
    DailyMetricReading.objects.update_or_create(
        metric=metric, date=today, slot=slot, defaults={"value": value},
    )
    return JsonResponse({"ok": True})


@require_daily_actor
@require_http_methods(["POST"])
def push_subscribe(request):
    """Store (or refresh) this device's Web Push subscription for the logged-in
    participant. Body = the browser's PushSubscription.toJSON()."""
    from .models import PushSubscription
    participant = request.daily_participant
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)
    endpoint = body.get("endpoint")
    keys = body.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return JsonResponse({"ok": False, "error": "incomplete"}, status=400)
    # Upsert by endpoint (unique per device); re-point to this participant if
    # the same device was previously another participant's (shared phone).
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"participant": participant, "p256dh": p256dh, "auth": auth, "fail_count": 0},
    )
    return JsonResponse({"ok": True})
