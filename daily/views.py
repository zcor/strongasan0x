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
    DAILY_TOKEN_COOKIE,
    DAILY_TOKEN_COOKIE_MAX_AGE,
    login_with_token,
    require_daily_actor,
    token_belongs_to_current_warrior,
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
from .services.ai_coach import CHECKLIST_SIZE
from .services.checklist import apply_pending_mutations, revert_to_baseline
from .services.coach_runner import coach_prior_day, run_coach
from .services.tz import participant_today

logger = logging.getLogger(__name__)

# Bonus section appears once this many core items are done. Proportional to the
# 3-item core (was 3-of-5; now 2-of-3) — you reveal the bonus zone with one core
# item left to go, so it never competes with an unfinished list.
BONUS_REVEAL_AT = 2


def _real_today(request) -> date:
    """The current LOCAL date for THIS request's participant (their timezone),
    falling back to the server tz when there's no participant on the request
    (e.g. token_login, which runs before @require_daily_actor) or no stored tz.
    This is the app's true "today" — every day-boundary decision keys off it."""
    participant = getattr(request, "daily_participant", None)
    if participant is not None:
        return participant_today(participant)
    return timezone.localdate()


def _resolve_today(request) -> date:
    real_today = _real_today(request)
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
    return _resolve_today(request) < _real_today(request) and bool(request.GET.get("day"))


def _as_of_query(request) -> str:
    day = request.GET.get("day")
    if day and _is_backfill(request):
        return f"?day={day}"
    if settings.DEBUG:
        val = request.GET.get("as_of")
        if val:
            return f"?as_of={val}"
    return ""


def _active_token(participant):
    """The participant's current (non-revoked) access token UUID, or None.
    Used to hand the token to the page's JS so the PWA can persist it to its
    own localStorage for iOS-standalone self-healing re-auth."""
    from .models import DailyAccessToken
    tok = (
        DailyAccessToken.objects
        .filter(participant=participant, revoked_at__isnull=True)
        .order_by("-created_at")
        .values_list("token", flat=True)
        .first()
    )
    return tok


def token_login(request, token):
    # A warrior who logged into the warrior dashboard carries a lingering
    # Telegram session. Only REFUSE the token login (409) if the token belongs
    # to a DIFFERENT participant — a genuine "someone else's link on my logged-in
    # browser" conflict. A warrior tapping THEIR OWN daily link must just work;
    # a PWA install has no incognito escape hatch, so refusing it would brick
    # them (this was Spencer's bug).
    if warrior_session_keys_set(request) and not token_belongs_to_current_warrior(request, token):
        return render(
            request,
            "daily/token_conflict.html",
            {"telegram_username": request.session.get("warrior_telegram_username", "")},
            status=409,
        )

    participant = login_with_token(request, token)
    if participant is None:
        return render(request, "daily/token_invalid.html", status=404)

    # Apply ?theme= here so it survives the redirect to /daily/checkin/.
    _resolve_theme(request)
    resp = redirect(f"/daily/checkin/{_as_of_query(request)}")
    # Drop a long-lived cookie with the token so an expired session silently
    # re-authenticates (require_daily_actor reads it) instead of dead-ending at
    # the warrior login — the fix that keeps re-engagement links working weeks on.
    resp.set_cookie(
        DAILY_TOKEN_COOKIE, str(token),
        max_age=DAILY_TOKEN_COOKIE_MAX_AGE,
        secure=True, httponly=True, samesite="Lax",
    )
    return resp


def ensure_prior_day_coached(participant: DailyParticipant, today: date) -> bool:
    """Lazy in-request coach: if the most recent check-in BEFORE today has no
    coach suggestion, run the coach for it synchronously (once). Thin wrapper
    over the shared coach_runner so the lazy path and the nightly cron share
    one implementation. Returns True if a coach run happened.
    """
    return coach_prior_day(participant, today)


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


VALID_THEMES = {"gauge", "steel"}


def _resolve_theme(request) -> str:
    """Theme for the page: ?theme=gauge|steel sets + remembers it in session;
    ?theme=default clears it. Empty string = the original look. Lets us A/B the
    visual identity on-device without per-user config."""
    raw = request.GET.get("theme")
    if raw is not None:
        # Store "" for default/anything-invalid; a real theme otherwise.
        chosen = raw if raw in VALID_THEMES else ""
        request.session["daily_theme"] = chosen
        request.session.modified = True
        return chosen
    return request.session.get("daily_theme", "") or ""


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
# app daily. Cap = 3 so the bar never exceeds a full core sweep (the core is 3).
STREAK_FLOOR = 1
STREAK_CAP = 3


def _day_done_count(ci: DailyCheckIn) -> int:
    """Items done that day, counting BOTH core and bonus."""
    return sum(1 for a in ci.answers.all() if a.state == DailyCheckInAnswer.STATE_DONE)


def _streak_bar(done_counts: list) -> int:
    """The per-user threshold: what this person reliably hits on a normal day.
    Use the MEDIAN of their recent daily done-counts (robust to one big/zero
    day), clamped to [FLOOR, CAP]. A user who routinely sweeps all 3 gets a bar
    of 3; one who routinely does 1-2 gets a bar of 1-2."""
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


def _needs_onboarding(participant) -> bool:
    """Whether to SHOW the onboarding tour. onboarded_at not set = show it once.
    Note: showing it and SEEDING from it are different — an attestation-warrior
    still sees the tour as a welcome, but their checklist is seeded from their
    logs, not the survey (see submit_onboarding / _has_attestation_history)."""
    return participant.onboarded_at is None


def _has_attestation_history(participant) -> bool:
    """True if this participant's Telegram identity has posted attestations.
    Attestation history is RICHER than a 3-question survey, so when present it
    drives the checklist and the survey only lightly steers the coach — the
    survey must NEVER overwrite an attestation-tailored list with a generic
    seeded set."""
    if not participant.telegram_mapping_id:
        return False
    from rollcall.models import Attestation
    return Attestation.objects.filter(
        telegram_user_id=participant.telegram_mapping_id
    ).exists()


@ensure_csrf_cookie  # page is AJAX-driven; force the csrftoken cookie even with no rendered form
@require_daily_actor
@require_http_methods(["GET"])
def checkin(request):
    participant: DailyParticipant = request.daily_participant
    today = _resolve_today(request)
    backfill = _is_backfill(request)

    # Naked first-timer → onboarding (skip the coach/version work below; there's
    # nothing to coach yet). A returning naked user who hasn't finished still
    # sees it until they submit or skip (both stamp onboarded_at).
    if _needs_onboarding(participant) and not backfill:
        from .services.onboarding import QUESTIONS
        # Q2's options are branch-dependent (chosen after Q1); ship them as JSON
        # for the client to inject. The other questions render server-side.
        q2 = next((q for q in QUESTIONS if q["id"] == "q2_focus"), {})
        return render(request, "daily/onboarding.html", {
            "participant": participant,
            "questions": QUESTIONS,
            "q2_branch_json": json.dumps(q2.get("branch_options", {})),
            "theme": _resolve_theme(request),
        })

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
        # "checklist updated" + Undo only when the CORE items actually changed
        # (bonus-only version churn reads as a plain note).
        if note and note.status == CoachSuggestion.STATUS_APPLIED and note.applied_version_id:
            parent = note.applied_version.derived_from
            note_core_changed = parent is None or parent.questions != note.applied_version.questions

    # The morning note becomes the first coach CHAT message of the day, so it
    # lives in the conversation thread (not a separate card). Seed it once.
    from .models import CoachChatMessage
    chat_history = []
    chat_unread = False
    if not backfill:
        if note and not CoachChatMessage.objects.filter(suggestion=note).exists():
            CoachChatMessage.objects.create(
                participant=participant, role=CoachChatMessage.ROLE_COACH,
                text=note.suggestion_text, date=today, suggestion=note,
            )
        chat_history = [
            {"role": m.role, "text": m.text, "at": m.created_at.strftime("%-I:%M %p")}
            for m in _chat_history(participant)
        ]
        # "Unread" = the most recent message is from the coach (a fresh note or
        # a reply the user hasn't opened the chat to see). Drives the FAB dot
        # and the load modal.
        chat_unread = bool(chat_history) and chat_history[-1]["role"] == "coach"
        latest_coach_text = chat_history[-1]["text"] if chat_unread else ""

    core_items = [
        {"key": q["key"], "label": q["label"], "state": states.get(q["key"], "pending")}
        for q in render_version.questions
    ]
    done_count = sum(1 for i in core_items if i["state"] == "done")

    bonus_items = [
        {
            "key": q["key"], "label": q["label"],
            "state": states.get(q["key"], "pending"),
            # Conditional bonus: only unlocks once `unlock_after` (a core/bonus
            # item key) is done. None = a normal, always-available bonus.
            "unlock_after": q.get("unlock_after"),
            "unlocked": (not q.get("unlock_after")) or states.get(q["unlock_after"]) == "done",
        }
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
    real_today = _real_today(request)
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        ci = by_date.get(d)
        total = len(ci.checklist_version.questions) if ci else CHECKLIST_SIZE
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
        "theme": _resolve_theme(request),
        "metric_fields": _metric_fields(participant, today) if not backfill else [],
        "wrapped": wrapped,
        "week": week,
        "note": note,
        "note_was_applied": note_core_changed,
        "chat_history": chat_history,
        "chat_unread": chat_unread,
        "latest_coach_text": locals().get("latest_coach_text", ""),
        "core_items": core_items,
        "bonus_items": bonus_items,
        # Reveal bonus once threshold hit — or keep visible if any bonus
        # already has a state (don't hide items the user interacted with).
        "bonus_revealed": bool(bonus_items) and (done_count >= BONUS_REVEAL_AT or bonus_done),
        "bonus_reveal_at": BONUS_REVEAL_AT,
        "checklist_size": CHECKLIST_SIZE,
        # The participant's own active token, so the page can persist it to the
        # PWA's OWN localStorage. On iOS a standalone (home-screen) PWA has a
        # SEPARATE cookie/storage jar from Safari: a token link tapped in Safari
        # logs in Safari, but the installed app icon never sees that session. By
        # stashing the token in the PWA's localStorage on a successful load, the
        # signed-out page can self-heal by re-hitting the token URL INSIDE the
        # PWA jar (where the cookie then sticks). See daily/templates signed_out.
        "self_token": str(_active_token(participant) or ""),
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
    coach as before.

    The wrap-up reply lives in the coach CHAT and invites evening planning
    (the frog-first "Plan tomorrow" flow) — unless the user already planned,
    in which case it says so honestly instead."""
    from .models import CoachChatMessage
    participant = request.daily_participant
    today = _resolve_today(request)
    if _is_backfill(request):
        return JsonResponse({"ok": False, "error": "no_wrap_in_backfill"}, status=400)

    version = participant.get_or_create_current_checklist()
    check_in = _get_or_create_today(participant, today, version)

    # Re-wrap: clear any prior un-applied reflection for today — but NEVER a
    # user-authored evening plan (wrapping after planning must not destroy it;
    # the coach run below writes its note AROUND the plan instead).
    check_in.suggestions.exclude(
        status__in=[CoachSuggestion.STATUS_APPLIED, CoachSuggestion.STATUS_DISMISSED]
    ).exclude(
        rationale=CoachSuggestion.RATIONALE_EVENING_PLAN
    ).update(status=CoachSuggestion.STATUS_DISMISSED, responded_at=timezone.now())

    _run_coach(check_in.id)
    ready = check_in.suggestions.exclude(
        status=CoachSuggestion.STATUS_DISMISSED
    ).exists()

    plan = check_in.suggestions.filter(
        rationale=CoachSuggestion.RATIONALE_EVENING_PLAN,
        status=CoachSuggestion.STATUS_PENDING,
        proposed_questions__isnull=False,
    ).order_by("-created_at").first()

    invite_planning = False
    if plan:
        frog = plan.proposed_questions[0]["label"]
        reply = f'Day wrapped. Tomorrow is already planned — "{frog}" first. Rest up.'
    elif ready:
        reply = (
            "Day wrapped — your morning note is set. Want to set up tomorrow? "
            "What's the ONE thing you've been putting off that would matter most?"
        )
        invite_planning = True
    else:
        reply = (
            "I couldn't wrap the day just now (coach is offline). Your taps "
            "are saved — tell me anything here and I'll fold it in overnight."
        )
    CoachChatMessage.objects.create(
        participant=participant, role=CoachChatMessage.ROLE_COACH,
        text=reply, date=today,
    )
    return JsonResponse({
        "ok": True, "note_ready": ready,
        "reply": reply, "invite_planning": invite_planning,
    })


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
    messages.success(request, "Checklist reset to the original 3 questions.")
    return redirect(f"/daily/checkin/{_as_of_query(request)}")


def _run_coach(check_in_id: int, refinement: str = ""):
    """Synchronous coach run (mod_wsgi-safe — daemon threads are not).
    Delegates to the shared coach_runner; kept as a wrapper because other
    views (wrap_day) call it by this name."""
    run_coach(check_in_id, refinement=refinement)


# --- Coach chat (live two-way; successor to the comment box) ----------------

ADMIN_TELEGRAM_ID = 1234982301  # CurveCap — gets a DM for each user chat message


def _notify_admin_of_message(participant, text):
    """Best-effort Telegram DM to the admin so feedback is never missed (the
    comment box used to be read by hand; chat keeps that). Never raises."""
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""
    if not token:
        return
    try:
        import requests
        msg = f"💬 {participant.display_name}: {text[:400]}"
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": ADMIN_TELEGRAM_ID, "text": msg},
            timeout=5,
        )
    except Exception:
        logger.warning("daily: admin chat notify failed", exc_info=True)


def _metrics_summary(participant):
    """Compact recent-metrics string for chat context, or ''."""
    from .models import DailyMetric, DailyMetricReading
    metrics = list(DailyMetric.objects.filter(participant=participant, is_active=True))
    if not metrics:
        return ""
    lines = []
    for m in metrics:
        last = (DailyMetricReading.objects.filter(metric=m).order_by("-date").first())
        if last:
            lines.append(f"  {m.label}: {last.value} ({last.date})")
    return "\n".join(lines)


def _chat_history(participant, limit=40):
    from .models import CoachChatMessage
    msgs = list(
        CoachChatMessage.objects.filter(participant=participant).order_by("-created_at")[:limit]
    )
    msgs.reverse()
    return msgs


@require_daily_actor
@require_http_methods(["POST"])
def chat_send(request):
    """User sends a chat message → save it, notify admin, generate + save a
    live coach reply, return the reply.

    planning=true in the body switches the coach to the evening "Plan
    tomorrow" mode: it shapes a frog-first 3-item list for tomorrow and, once
    settled, emits it as JSON. We parse that here and queue it as a pending
    CoachSuggestion on today's check-in — the SAME auto-apply path every coach
    mutation already rides — and only then does the reply claim "locked in"
    (the confirmation line is appended by THIS code after the queue genuinely
    succeeded, never by the model)."""
    from .models import CoachChatMessage
    from .services.ai_coach import chat_reply, parse_planned_list
    participant = request.daily_participant
    today = _resolve_today(request)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)
    text = str(body.get("text", "")).strip()
    planning = bool(body.get("planning"))
    if not text:
        return JsonResponse({"ok": False, "error": "empty"}, status=400)
    if len(text) > 2000:
        text = text[:2000]

    user_msg = CoachChatMessage.objects.create(
        participant=participant, role=CoachChatMessage.ROLE_USER, text=text, date=today,
    )
    _notify_admin_of_message(participant, text)
    user_msg.notified = True
    user_msg.save(update_fields=["notified"])

    # Build context + history and get a live reply.
    version = participant.get_or_create_current_checklist()
    ci = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    states = ci.answers_by_key() if ci else {}
    recent = DailyCheckIn.objects.filter(
        participant=participant, date__lt=today
    ).order_by("-date")[:3]
    recent_summary = "; ".join(
        f"{r.date}: {r.score}/{len(r.checklist_version.questions)}" for r in recent
    )
    history = [
        {"role": m.role, "text": m.text} for m in _chat_history(participant)
    ]
    result = chat_reply(
        participant.display_name, list(version.questions), states,
        _metrics_summary(participant), recent_summary, history,
        planning=planning,
    )
    planned = False
    if result is None:
        reply_text = "Got it — logged. (Coach is offline right now, but I saved your note.)"
        model = ""
    else:
        reply_text, model, _cost = result
        if planning:
            display, items = parse_planned_list(reply_text)
            had_json = display != reply_text.strip()
            if items is not None and _queue_evening_plan(participant, today, items, model):
                planned = True
                confirm = (
                    f'Locked in for tomorrow morning — "{items[0]["label"]}" '
                    "leads, then the other two. You'll see it when you open "
                    "the app."
                )
                reply_text = f"{display}\n\n{confirm}".strip() if display else confirm
            elif had_json:
                # The model tried to emit a plan but it didn't validate/queue.
                # Never show raw JSON, never claim success — say so honestly.
                miss = "(I couldn't lock that in — give me the three again and I'll retry.)"
                reply_text = f"{display}\n\n{miss}".strip() if display else miss
            # else: still conversing (no JSON yet) — pass the reply through.
    coach_msg = CoachChatMessage.objects.create(
        participant=participant, role=CoachChatMessage.ROLE_COACH,
        text=reply_text, date=today,
    )
    return JsonResponse({
        "ok": True,
        "planned": planned,
        "reply": {"text": coach_msg.text, "at": coach_msg.created_at.isoformat()},
    })


def _queue_evening_plan(participant, today, items, model_name=""):
    """Queue a user-authored evening plan as a pending CoachSuggestion on
    TODAY's check-in — proposed_questions = their 3 items, frog first. The
    EXISTING auto-apply machinery (apply_pending_mutations, which only touches
    suggestions from days BEFORE "today") promotes it tomorrow morning; the
    overnight coach sees the pending plan and writes its note around it
    instead of competing (see coach_runner.run_coach).

    Supersedes any other queued-but-unapplied mutation on today's check-in
    (an earlier plan tonight, a wrap-generated mutation) so exactly one
    mutation is ever pending — double-application can't happen.

    suggestion_text doubles as the fallback morning note if the overnight
    note-around-the-plan run never happens. Returns the suggestion (truthy)
    or None.
    """
    if not items:
        return None
    version = participant.get_or_create_current_checklist()
    check_in = _get_or_create_today(participant, today, version)
    check_in.suggestions.filter(
        proposed_questions__isnull=False,
    ).exclude(
        status__in=[
            CoachSuggestion.STATUS_APPLIED,
            CoachSuggestion.STATUS_DISMISSED,
            CoachSuggestion.STATUS_SHOWN,  # already-evaluated no-ops can't apply
        ]
    ).update(status=CoachSuggestion.STATUS_DISMISSED, responded_at=timezone.now())

    frog = items[0]["label"]
    note = (
        f'Your plan, set last night: "{frog}" comes first — that\'s the frog, '
        "eat it while you're fresh. The other two are there to back it up."
    )
    return CoachSuggestion.objects.create(
        check_in=check_in,
        suggestion_text=note,
        proposed_questions=list(items),
        rationale=CoachSuggestion.RATIONALE_EVENING_PLAN,
        status=CoachSuggestion.STATUS_PENDING,
        model_name=model_name or "",
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
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
// Deliberately NO fetch handler. An earlier version intercepted every
// navigation with event.respondWith(fetch(...)) to show an offline message —
// but that put the SW in the critical path of every page load with no timeout,
// so a slow/cold SW wakeup stalled loads (~20s observed). The page is fast on
// its own; the SW only needs to exist for installability + push. Not proxying
// navigations = the browser loads directly, instantly.

// --- Web Push: the morning badge ---
// The server pushes {count: N} each morning. We set the home-screen badge to
// N (today's remaining to-dos) WITHOUT opening the app. iOS requires us to
// also show a notification on each push, so we show a quiet one whose body
// just states the count — it doubles as the morning nudge.
//
// --- Web Push: the evening "Plan tomorrow" nudge ---
// A separate, independent push (send_evening_plan_nudge.py) carries
// {kind: 'evening_nudge', title, body} instead of {count}. It never touches
// the app badge — it's a plain reminder notification, distinguished by
// `kind` so this one handler can serve both jobs without either stepping on
// the other's payload shape.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = (event.data && event.data.json()) || {}; } catch (e) {}

  if (data.kind === 'evening_nudge') {
    // Unique per-day tag + renotify so it always alerts fresh and iOS never
    // silently REPLACES a same-tag notification from a prior day (that silent
    // swap is why a fixed-tag push can arrive without ever alerting).
    event.waitUntil(self.registration.showNotification(data.title || 'Plan tomorrow', {
      body: data.body || "Set tomorrow's 3 and wake up ready to win.",
      badge: '/static/daily/icons/icon-192.png',
      icon: '/static/daily/icons/icon-192.png',
      tag: 'daily-evening-nudge-' + (data.day || ''), renotify: true,
    }));
    return;
  }

  const count = data.count || 0;
  event.waitUntil((async () => {
    try {
      if (navigator.setAppBadge) {
        if (count > 0) await navigator.setAppBadge(count); else await navigator.clearAppBadge();
      }
    } catch (e) {}
    // iOS will not deliver a silent push reliably — a visible notification is
    // required. Keep it minimal and on-message (it IS the daily counter).
    // Per-day tag + renotify:true so each morning's badge alerts fresh instead
    // of silently replacing the prior day's same-tag notification.
    const title = count > 0 ? (count + ' to-do' + (count === 1 ? '' : 's') + ' today') : 'All done for today';
    const body = count > 0 ? 'Open Daily and fill your rings.' : 'Nice work — see you tomorrow.';
    await self.registration.showNotification(title, {
      body: body, badge: '/static/daily/icons/icon-192.png',
      icon: '/static/daily/icons/icon-192.png',
      tag: 'daily-badge-' + (data.day || ''), renotify: true,
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
    """How many CORE items the participant still has to do today (0..core
    size). This is the number the home-screen badge shows. A day with no
    check-in yet = the full core count of their current checklist."""
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


@require_daily_actor
@require_http_methods(["POST"])
def set_timezone(request):
    """Capture the participant's browser timezone (IANA name) so the app's day
    boundary — today, streak, badge reset — is THEIR local day, not the server
    tz. The page POSTs Intl.DateTimeFormat().resolvedOptions().timeZone on load;
    we save it only when it's a valid IANA name and actually changed."""
    from .services.tz import is_valid_iana
    participant = request.daily_participant
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)
    tz = str(body.get("timezone", "")).strip()
    if not is_valid_iana(tz):
        return JsonResponse({"ok": False, "error": "bad_timezone"}, status=400)
    if participant.timezone != tz:
        participant.timezone = tz
        participant.save(update_fields=["timezone", "updated_at"])
    return JsonResponse({"ok": True})


@require_daily_actor
@require_http_methods(["POST"])
def submit_onboarding(request):
    """Finish onboarding and stamp onboarded_at so it never shows again.

    What the survey answers DO depends on whether we have richer data:
      - TRUE naked user (no attestations): the answers SEED their 3-item list.
      - Attestation-warrior: their list is already tailored from their logs, so
        the survey must NOT overwrite it — it's recorded as a coach note that
        lightly steers the overnight coach instead.
    A pure skip just stamps onboarded_at and leaves the current list untouched."""
    from .services.onboarding import seed_questions, survey_summary
    participant = request.daily_participant
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    # Guard: only an actually-naked user may (re)seed via onboarding. An
    # already-onboarded user hitting this endpoint (double-submit, direct POST)
    # must NOT overwrite their personalized checklist — just bounce them home.
    if not _needs_onboarding(participant):
        return JsonResponse({"ok": True, "redirect": "/daily/checkin/"})

    has_history = _has_attestation_history(participant)

    if not body.get("skip"):
        branch = str(body.get("q1_goal", "")).strip() or None
        focus = str(body.get("q2_focus", "")).strip() or None
        cadence = str(body.get("q3_cadence", "")).strip() or None

        # CRUCIAL: attestation history is richer than a 3-question survey. For a
        # warrior, the checklist is already tailored from their logs (pre-build /
        # lazy coach) — the survey must NOT overwrite it with a generic seeded
        # set. It only LIGHTLY STEERS: we record the answers as a coach note so
        # the overnight coach can lean that way over time. Only a TRUE naked user
        # (no attestations) has their list seeded from the survey.
        if not has_history:
            questions = seed_questions(branch, focus, cadence)
            with transaction.atomic():
                participant.checklist_versions.filter(is_current=True).update(is_current=False)
                ChecklistVersion.objects.create(
                    participant=participant, questions=questions,
                    source=ChecklistVersion.SOURCE_BASELINE, is_current=True,
                )

        # Log the survey (answers + any write-ins) as the day's first user chat
        # message so build_coach_context surfaces it — for a warrior this is the
        # gentle steer on top of their attestation-tailored list; for a stranger
        # it's extra colour beside the seeded list. Best-effort.
        write_ins = body.get("write_ins") or []
        note = survey_summary(branch, focus, cadence,
                              write_ins if isinstance(write_ins, list) else [])
        if note:
            from .models import CoachChatMessage
            CoachChatMessage.objects.create(
                participant=participant, role=CoachChatMessage.ROLE_USER,
                text=note, date=_resolve_today(request),
            )

    if participant.onboarded_at is None:
        participant.onboarded_at = timezone.now()
        if not participant.source:
            participant.source = "onboarding"
        participant.save(update_fields=["onboarded_at", "source", "updated_at"])
    return JsonResponse({"ok": True, "redirect": "/daily/checkin/"})
