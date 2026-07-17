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
import uuid
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.utils import timezone
from django.middleware.csrf import get_token
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
    WinItem,
)
from .services.ai_coach import CHECKLIST_SIZE
from .services.checklist import (
    MAX_CHECKLIST_SIZE,
    all_version_keys,
    apply_pending_mutations,
    dismiss_pending_mutations,
    habit_days,
    habit_step_rule,
    habit_steps_complete,
    health_bonus_items,
    revert_to_baseline,
    scheduled_questions,
    valid_habit_days,
    valid_habit_step_rule,
)
from .services.coach_runner import coach_prior_day, run_coach
from .services.streaks import current_streak, refresh_streak_cache
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


def _is_beta(request, participant) -> bool:
    """Whether to show the new Climb (beta) experience for this request.

    Normally just participant.beta. In LOCAL DEV (DEBUG=True) it can be toggled
    without touching the DB: hit `?beta=1` or `?beta=0` on any page and the
    choice is remembered in the session, so you can flip between the frozen
    current app and the beta redesign while developing. `?beta=` (empty) clears
    the override and falls back to the real flag. Production ignores all of
    this and honors only participant.beta.
    """
    if settings.DEBUG:
        override = request.GET.get("beta")
        if override is not None:
            if override in ("1", "true", "on"):
                request.session["daily_beta_override"] = True
            elif override in ("0", "false", "off"):
                request.session["daily_beta_override"] = False
            else:  # empty / anything else clears the override
                request.session.pop("daily_beta_override", None)
        stored = request.session.get("daily_beta_override")
        if stored is not None:
            return bool(stored)
    return bool(getattr(participant, "beta", False))


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

    _resolve_theme(request)
    # Render check-in IN PLACE at /daily/c/<token>/ — do NOT redirect to the
    # tokenless /daily/checkin/. On iOS a standalone PWA has its own cookie/
    # storage jar; when a user does "Add to Home Screen" from this page, iOS
    # captures the CURRENT (token-bearing) URL + this page's per-token manifest
    # as the launch URL, so the installed icon cold-starts authenticated inside
    # its own jar. A redirect here would strip the token from the captured URL
    # and re-create the broken tokenless icon (Spencer's bug).
    request.daily_participant = participant
    resp = _render_checkin(request, from_token=str(token))
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
        .select_related("check_in", "applied_version__derived_from")
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


def _habit_json(question, for_date, states=None):
    """Client/render payload for one habit, including its weekly schedule."""
    states = states or {}
    sub_items = [
        {
            "key": item["key"],
            "label": item["label"],
            "state": states.get(item["key"], "pending"),
        }
        for item in (question.get("items") or [])
    ]
    if sub_items:
        state = (
            "done"
            if (
                habit_steps_complete(question, states)
                or states.get(question["key"]) == "done"
            )
            else "pending"
        )
    else:
        state = states.get(question["key"], "pending")
    days = list(habit_days(question))
    return {
        "key": question["key"],
        "label": question["label"],
        "state": state,
        "items": sub_items,
        "days": days,
        "step_rule": habit_step_rule(question),
        "scheduled_today": for_date.weekday() in days,
        "core": True,
    }


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
    """The /daily/checkin/ page. @require_daily_actor has resolved the
    participant; render the page (no per-token manifest — this URL is
    tokenless)."""
    return _render_checkin(request, from_token="")


def _render_checkin(request, from_token=""):
    """Shared check-in page render. Called by `checkin` (tokenless URL) and by
    `token_login` (in-place at /daily/c/<token>/). `from_token` non-empty means
    we're rendering AT the token URL: the page then links a per-token manifest
    so an iOS "Add to Home Screen" captures a token-bearing launch URL. The
    caller must have set request.daily_participant."""
    # Guarantee the csrftoken cookie on EVERY render path (the page is
    # AJAX-driven; all its POSTs read the cookie). The `checkin` view gets this
    # from @ensure_csrf_cookie, but token_login calls us directly and bypasses
    # that decorator — without this, a freshly-installed token user (baseline or
    # naked/onboarding, whose templates don't render a {% csrf_token %} tag) has
    # NO cookie and every button POSTs 403. get_token() forces it here so the
    # guarantee lives with the render, not incidentally on a form tag.
    get_token(request)
    participant: DailyParticipant = request.daily_participant
    # DEV route toggles (local only), flipped by typing the query param; the
    # session remembers them. Processed BEFORE _is_beta so a flip takes effect
    # this render.
    if settings.DEBUG:
        _g = request.GET.get("gate")
        if _g == "1":
            request.session["daily_gate_forced"] = True
        elif _g == "0":
            request.session.pop("daily_gate_forced", None)
        _s = request.GET.get("streak")
        if _s == "1":
            request.session["daily_streak_forced"] = True
        elif _s == "0":
            request.session.pop("daily_streak_forced", None)
        _n = request.GET.get("notification_preview")
        if _n == "1":
            request.session["daily_notification_preview"] = True
        elif _n == "0":
            request.session.pop("daily_notification_preview", None)
    today = _resolve_today(request)
    backfill = _is_backfill(request)
    is_beta = _is_beta(request, participant)
    # Beta support-only Jamie (mutations off) does NOT run the overnight
    # list-rewriting engine; her value is live encouragement, not silent edits.
    run_engine = not (is_beta and not participant.ai_mutations_enabled)

    # Naked first-timer → onboarding (skip the coach/version work below; there's
    # nothing to coach yet). A returning naked user who hasn't finished still
    # sees it until they submit or skip (both stamp onboarded_at).
    if _needs_onboarding(participant) and not backfill:
        if is_beta:
            # Beta: the one-card onboarding (fork + focus question), NOT the
            # legacy 3-question survey. Current path is untouched below.
            return render(request, "daily/onboarding_beta.html", {
                "participant": participant,
                "theme": _resolve_theme(request),
                "self_token": str(_active_token(participant) or ""),
                "beta_toggle": settings.DEBUG,
            })
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

    if not backfill and run_engine:
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
    # The visible page only needs today plus the six preceding circles. Long
    # streak history is maintained as compact persisted rollups on answer
    # writes, rather than loading full historical objects on every dashboard.
    history_start = today - timedelta(days=6)
    active_suggestions = CoachSuggestion.objects.filter(
        check_in_id=OuterRef("pk")
    ).exclude(status=CoachSuggestion.STATUS_DISMISSED)
    recent_checkins = {
        check_in.date: check_in
        for check_in in DailyCheckIn.objects.filter(
            participant=participant,
            date__gte=history_start,
            date__lte=today,
        )
        .select_related("checklist_version")
        .annotate(has_active_suggestion=Exists(active_suggestions))
        .prefetch_related("answers")
    }
    existing = recent_checkins.get(today)
    states = existing.answers_by_key() if existing else {}

    # A past day renders with the checklist it actually had that day.
    render_version = existing.checklist_version if (backfill and existing) else current_version

    note = None
    note_core_changed = False
    note_should_present = False
    # The beta UI only shows coach notes inside its chat sheet, so resolving
    # and marking a note here would put chat work back on the dashboard's
    # critical path. The lazy chat_history endpoint does it on first open.
    # Legacy still needs the note immediately for its load modal.
    if not backfill and not is_beta:
        note = _morning_note(participant, today)
        if note:
            from .models import CoachChatMessage
            note_should_present = not CoachChatMessage.objects.filter(
                participant=participant,
                suggestion=note,
            ).exists()
        if note_should_present:
            if note.status == CoachSuggestion.STATUS_PENDING:
                note.status = CoachSuggestion.STATUS_SHOWN
                note.save(update_fields=["status"])
            # A report-linked row is the durable one-time delivery marker. It
            # is filtered out of conversation queries below, so it never
            # appears as a chat bubble.
            CoachChatMessage.objects.create(
                participant=participant,
                role=CoachChatMessage.ROLE_COACH,
                text=note.suggestion_text,
                date=today,
                suggestion=note,
            )
        # "checklist updated" + Undo only when the CORE items actually changed
        # (bonus-only version churn reads as a plain note).
        if note and note.status == CoachSuggestion.STATUS_APPLIED and note.applied_version_id:
            parent = note.applied_version.derived_from
            note_core_changed = parent is None or parent.questions != note.applied_version.questions

    # A morning report is a one-time dialog, not a chat message. Conversation
    # history contains only messages the user and Jamie actually exchanged.
    chat_history = []
    chat_unread = False
    if not backfill and not is_beta:
        chat_messages = _chat_history(participant)
        chat_history = [
            {"role": m.role, "text": m.text, "at": m.created_at.strftime("%-I:%M %p")}
            for m in chat_messages
        ]
        chat_unread = note_should_present
        latest_coach_text = note.suggestion_text if note_should_present else ""

    # Core habits, each optionally carrying a nested detail checklist
    # (`items`). A habit WITH sub-items derives its own done state using its
    # saved any/all step rule (see set_item_state, which persists that so the
    # ring/score math counts the parent, never the sub-items). Habits without
    # sub-items are unchanged.
    visible_questions = (
        scheduled_questions(render_version.questions, today)
        if is_beta
        else render_version.questions
    )
    core_items = [_habit_json(q, today, states) for q in visible_questions]
    done_count = sum(1 for i in core_items if i["state"] == "done")

    visible_bonus_questions = (
        health_bonus_items(render_version.bonus_questions)
        if is_beta
        else (render_version.bonus_questions or [])
    )
    bonus_items = [
        {
            "key": q["key"], "label": q["label"],
            "state": states.get(q["key"], "pending"),
            # Conditional bonus: only unlocks once `unlock_after` (a core/bonus
            # item key) is done. None = a normal, always-available bonus.
            "unlock_after": q.get("unlock_after"),
            "unlocked": (not q.get("unlock_after")) or states.get(q["unlock_after"]) == "done",
        }
        for q in visible_bonus_questions
    ]
    bonus_done = any(i["state"] != "pending" for i in bonus_items)

    # First-run intro: shown until the participant has marked anything, ever.
    # ?intro=1 force-shows it on demand (preview the onboarding anytime).
    has_recent_activity = any(
        answer.state != DailyCheckInAnswer.STATE_PENDING
        for check_in in recent_checkins.values()
        for answer in check_in.answers.all()
    )
    first_visit = not has_recent_activity and not DailyCheckInAnswer.objects.filter(
        check_in__participant=participant,
        check_in__date__lt=history_start,
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
    wrapped = bool(existing and existing.has_active_suggestion)

    # Last 7 days (oldest → today) for the week strip. Each day scored
    # against the checklist version that was active THAT day.
    week = []
    by_date = recent_checkins
    MINI_C = 62.8  # 2πr for the strip's r=10 mini-rings
    real_today = _real_today(request)
    # Beta: a gold star marks the win selected for that day and checked off.
    # North-star summit days also star — and they cover history: wins
    # completed before selection dates were retained have surfaced_on=None,
    # so goal done_at days are the only record of pre-existing stars.
    dashboard_wins = None
    if is_beta:
        from .services.wins import get_dashboard_wins, north_star_done_dates
        dashboard_wins = get_dashboard_wins(
            participant,
            today - timedelta(days=6),
            today,
            auto_select=not backfill,
        )
        win_days = dashboard_wins["done_dates"] | north_star_done_dates(
            participant, today - timedelta(days=6), today,
        )
    else:
        win_days = set()
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        ci = by_date.get(d)
        if ci:
            day_questions = (
                scheduled_questions(ci.checklist_version.questions, d)
                if is_beta
                else ci.checklist_version.questions
            )
            day_states = ci.answers_by_key()
            total = len(day_questions)
            done = sum(
                1 for question in day_questions
                if day_states.get(question["key"]) == DailyCheckInAnswer.STATE_DONE
            )
        else:
            total = (
                len(scheduled_questions(current_version.questions, d))
                if is_beta
                else CHECKLIST_SIZE
            )
            done = 0
        week.append({
            "label": d.strftime("%a")[0],
            "done": done,
            "total": total,
            "offset": round(MINI_C * (1 - done / total), 1) if total else MINI_C,
            "is_today": d == today,
            # A selected Today's Win was checked off on this day.
            "todays_win_done": d in win_days,
            # Tap a past day to backfill it; today links back to the plain page.
            "href": "/daily/checkin/" if d == real_today else f"/daily/checkin/?day={d.isoformat()}",
        })

    active_token = _active_token(participant)
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
        "max_checklist_size": MAX_CHECKLIST_SIZE,
        # The participant's own active token, so the page can persist it to the
        # PWA's OWN localStorage. On iOS a standalone (home-screen) PWA has a
        # SEPARATE cookie/storage jar from Safari: a token link tapped in Safari
        # logs in Safari, but the installed app icon never sees that session. By
        # stashing the token in the PWA's localStorage on a successful load, the
        # signed-out page can self-heal by re-hitting the token URL INSIDE the
        # PWA jar (where the cookie then sticks). See daily/templates signed_out.
        "self_token": str(active_token or ""),
        # Non-empty ONLY when rendering AT the token URL (/daily/c/<token>/).
        # Drives a per-token <link rel=manifest> whose start_url is the token
        # URL, so an iOS "Add to Home Screen" from here installs a token-bearing
        # icon that cold-launches authenticated in the PWA's own jar.
        "from_token": from_token,
        "done_count": done_count,
        "streak": current_streak(participant, today) if not backfill else 0,
        "vapid_public_key": getattr(settings, "VAPID_PUBLIC_KEY", ""),
        "comment": existing.comment if existing else "",
        "is_baseline": _is_baseline_questions(current_version.questions),
        "debug_as_of": request.GET.get("as_of") if settings.DEBUG else None,
        "tomorrow_qs": f"?as_of={(today + timedelta(days=1)).isoformat()}" if settings.DEBUG else "",
        "yesterday_qs": f"?as_of={(today - timedelta(days=1)).isoformat()}" if settings.DEBUG else "",
        # In DEBUG, the beta toggle is on: the page shows which mode it's in and
        # how to flip it (see _is_beta). Always False in production.
        "beta_toggle": settings.DEBUG,
        # DEV: whether the install/notification gate is force-shown (session).
        "dev_gate": settings.DEBUG and request.session.get("daily_gate_forced", False),
        "dev_notification_preview": settings.DEBUG and request.session.get("daily_notification_preview", False),
    }
    if is_beta:
        # The combined win + habit screen. Add the wins-facet context; the habit
        # core context above is shared with the frozen path.
        todays_win = dashboard_wins["selected"] if dashboard_wins and not backfill else None
        completed_todays_win = (
            dashboard_wins["completed"] if dashboard_wins and not backfill else None
        )
        context["is_beta"] = True
        context["todays_win"] = _win_json(todays_win)
        context["completed_todays_win"] = _win_json(completed_todays_win)
        context["ai_mutations_enabled"] = participant.ai_mutations_enabled
        # DEV: force-show the streak pill even when the real streak is < 2, so its
        # look can be previewed on a fresh account. Keeps a real streak (>=2)
        # honest; only fabricates a demo number when there's nothing to show.
        if settings.DEBUG and request.session.get("daily_streak_forced", False):
            if context["streak"] < 2:
                context["streak"] = 3
        return render(request, "daily/checkin_beta.html", context)
    context["is_beta"] = False
    return render(request, "daily/checkin.html", context)


def _derive_parent(check_in, parent_question, states):
    """Re-derive a habit after a small-step change using its saved rule.

    ``any`` fills the parent after one step; ``all`` waits for every step.
    A parent checked directly first checks all of its steps, so later step
    changes can always apply this rule consistently. Updates `states` in place
    and returns {key,state} for the client."""
    parent_key = parent_question["key"]
    existing = DailyCheckInAnswer.objects.filter(
        check_in=check_in, question_key=parent_key
    ).first()
    if habit_steps_complete(parent_question, states):
        parent_state = DailyCheckInAnswer.STATE_DONE
        if existing is None or existing.state != parent_state:
            DailyCheckInAnswer.objects.update_or_create(
                check_in=check_in, question_key=parent_key,
                defaults={"state": parent_state, "derived": True},
            )
    elif (
        not parent_question.get("items")
        and existing is not None
        and existing.state == DailyCheckInAnswer.STATE_DONE
        and not existing.derived
    ):
        # Removing the final small step should not erase a direct manual mark.
        # (While steps EXIST they are the source of truth — a direct parent
        # tap cascades them all to done, so re-deriving is always consistent.)
        parent_state = DailyCheckInAnswer.STATE_DONE
    else:
        parent_state = DailyCheckInAnswer.STATE_PENDING
        DailyCheckInAnswer.objects.update_or_create(
            check_in=check_in, question_key=parent_key,
            defaults={"state": parent_state, "derived": True},
        )
    states[parent_key] = parent_state
    return {"key": parent_key, "state": parent_state}


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
    beta_mode = _is_beta(request, participant)

    body, err = _json_body(request)
    if err:
        return err
    key = str(body.get("key", "")).strip()
    state = str(body.get("state", "")).strip()
    custom_label = str(body.get("label", "")).strip()  # swap → "Write my own"

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
    # Every sub-item key maps back to its parent habit, for the nested detail
    # checklists. Sub-item keys are valid targets too.
    subkey_to_parent = {}
    for q in version.questions:
        for s in (q.get("items") or []):
            subkey_to_parent[s["key"]] = q["key"]

    active_bonus_questions = (
        health_bonus_items(version.bonus_questions)
        if beta_mode
        else (version.bonus_questions or [])
    )
    valid_keys = set(version.question_keys()) | {
        q["key"] for q in active_bonus_questions
    } | set(subkey_to_parent)
    if key not in valid_keys:
        return JsonResponse({"ok": False, "error": "bad_key"}, status=400)

    bonus_keys = {q["key"] for q in active_bonus_questions}
    is_bonus = key in bonus_keys
    is_sub = key in subkey_to_parent

    with transaction.atomic():
        check_in = _get_or_create_today(participant, today, version)
        DailyCheckInAnswer.objects.update_or_create(
            check_in=check_in,
            question_key=key,
            defaults={"state": state, "derived": False},  # a direct user tap
        )

    check_in.refresh_from_db()
    states = check_in.answers_by_key()

    # A small-step toggle re-derives its parent using that habit's saved
    # any/all rule. Persist it so the ring counts the parent, never the steps.
    parent_update = None
    if is_sub:
        parent_key = subkey_to_parent[key]
        parent_q = next(q for q in version.questions if q["key"] == parent_key)
        parent_update = _derive_parent(check_in, parent_q, states)
    elif not is_bonus:
        parent_q = next((q for q in version.questions if q["key"] == key), None)
        sub_keys = [s["key"] for s in ((parent_q or {}).get("items") or [])]
        if sub_keys and state in (
            DailyCheckInAnswer.STATE_DONE,
            DailyCheckInAnswer.STATE_PENDING,
            DailyCheckInAnswer.STATE_SKIP,
        ):
            # The parent checkbox is a check-all/clear-all shortcut. That keeps
            # an "All steps" habit honest while preserving a convenient way to
            # finish or reset the full group in one tap.
            sub_state = (
                DailyCheckInAnswer.STATE_DONE
                if state == DailyCheckInAnswer.STATE_DONE
                else DailyCheckInAnswer.STATE_PENDING
            )
            with transaction.atomic():
                for sub_key in sub_keys:
                    DailyCheckInAnswer.objects.update_or_create(
                        check_in=check_in,
                        question_key=sub_key,
                        defaults={"state": sub_state, "derived": True},
                    )
            for sk in sub_keys:
                states[sk] = sub_state

    core_questions = (
        scheduled_questions(version.questions, today)
        if beta_mode
        else version.questions
    )
    core_keys = [question["key"] for question in core_questions]
    done_count = sum(1 for k in core_keys if states.get(k) == "done")
    # Persist the daily rollup and participant streak after every check,
    # uncheck, skip, or backfill. This keeps dashboard GETs history-free.
    refresh_streak_cache(
        participant,
        today=_real_today(request),
        changed_check_in=check_in,
        done_count=sum(
            1 for answer_state in states.values()
            if answer_state == DailyCheckInAnswer.STATE_DONE
        ),
    )

    # Live drip triggers (never in backfill):
    #  (a) completed a BONUS → refill the pile with a fresh one
    #  (b) SWAPPED any item (state=skip = "not interested") → generate a
    #      replacement in place; core swaps keep the ring completable
    #  (c) reached the core threshold with no bonus yet → first bonus
    new_bonus = None
    replacement = None
    if not backfill:
        if state == DailyCheckInAnswer.STATE_SKIP and not is_sub:
            if custom_label:
                # User chose "Write my own" on swap → use their text, no AI.
                replacement = _replace_item_custom(version, rejected_key=key, label=custom_label, is_core=not is_bonus)
            else:
                replacement = _replace_item(
                    participant, version, check_in, states,
                    rejected_key=key, is_core=not is_bonus,
                    health_only=beta_mode and is_bonus,
                )
        elif is_bonus and state == DailyCheckInAnswer.STATE_DONE:
            new_bonus = _generate_and_append_bonus(
                participant, version, check_in, states, health_only=beta_mode,
            )
        elif (
            not is_bonus
            and state == DailyCheckInAnswer.STATE_DONE
            and done_count >= BONUS_REVEAL_AT
            and not (
                health_bonus_items(version.bonus_questions)
                if beta_mode
                else version.bonus_questions
            )
        ):
            new_bonus = _generate_and_append_bonus(
                participant, version, check_in, states, health_only=beta_mode,
            )

    version.refresh_from_db()
    core_questions = (
        scheduled_questions(version.questions, today)
        if beta_mode
        else version.questions
    )
    core_keys = [question["key"] for question in core_questions]
    done_count = sum(1 for k in core_keys if states.get(k) == "done")
    return JsonResponse({
        "ok": True,
        "done_count": done_count,
        "bonus_revealed": bool(
            health_bonus_items(version.bonus_questions)
            if beta_mode
            else version.bonus_questions
        ) and done_count >= BONUS_REVEAL_AT,
        "new_bonus": new_bonus,            # {key,label} to append, or null
        "replacement": replacement,        # {key,label,core} swapped in place, or null
        "replaced_key": key if replacement else None,
        "parent": parent_update,           # {key,state} re-derived parent, or null
    })


def _ai_grounding(participant, version, states):
    """The context every live one-item AI call shares: everything already on
    the list, today's done/skipped labels (all, and bonus-only), and the
    user's recent attestation text."""
    from rollcall.models import Attestation

    existing = list(version.questions) + list(version.bonus_questions or [])
    label_by_key = {q["key"]: q["label"] for q in existing}
    bonus_keys = {q["key"] for q in (version.bonus_questions or [])}
    done = [label_by_key[k] for k, s in states.items() if s == "done" and k in label_by_key]
    skipped = [label_by_key[k] for k, s in states.items() if s == "skip" and k in label_by_key]
    skipped_bonus = [label_by_key[k] for k, s in states.items() if s == "skip" and k in bonus_keys]

    att_text = ""
    if participant.telegram_mapping_id:
        atts = Attestation.objects.filter(
            telegram_user_id=participant.telegram_mapping_id
        ).order_by("-posted_at")[:3]
        att_text = "\n\n".join(a.raw_text for a in atts)
    return {
        "existing": existing, "done": done, "skipped": skipped,
        "skipped_bonus": skipped_bonus, "att": att_text,
    }


def _swap_or_append(version, item, rejected_key=None, is_core=True):
    """Atomically put `item` on the version: substituted for `rejected_key`,
    or appended when rejected_key is None (core list or bonus pile). Returns
    the item, or None on a key collision (checked against every key in play,
    sub-items included)."""
    with transaction.atomic():
        v = ChecklistVersion.objects.select_for_update().get(id=version.id)
        incoming_keys = {item["key"]} | {
            subitem["key"] for subitem in (item.get("items") or [])
        }
        if (
            len(incoming_keys) != 1 + len(item.get("items") or [])
            or incoming_keys & all_version_keys(v)
        ):
            return None  # collision guard
        if is_core:
            if rejected_key is None:
                v.questions = list(v.questions) + [item]
            else:
                v.questions = [item if q["key"] == rejected_key else q for q in v.questions]
            v.save(update_fields=["questions"])
        else:
            bonus = [b for b in (v.bonus_questions or []) if b["key"] != rejected_key]
            bonus.append(item)
            v.bonus_questions = bonus
            v.save(update_fields=["bonus_questions"])
    return item


def _replace_item(
    participant, version, check_in, states, rejected_key, is_core,
    health_only=False,
):
    """User tapped "swap" — generate a grounded replacement and substitute
    it in the current version (core slot or bonus pile). Returns
    {key,label,core} or None (row stays struck-through as a fallback)."""
    from .services.ai_coach import generate_one_bonus

    g = _ai_grounding(participant, version, states)
    item = generate_one_bonus(
        participant_name=participant.display_name,
        attestation_text=g["att"],
        existing_items=g["existing"],
        today_done_labels=g["done"],
        today_comment=check_in.comment or "",
        rejected_labels=g["skipped"],
        core=is_core,
        health_only=health_only,
    )
    if item is None or _swap_or_append(version, item, rejected_key, is_core) is None:
        return None
    return {**item, "core": is_core}


def _new_user_key() -> str:
    """Stable, collision-resistant key for a user-authored item."""
    return "u_" + uuid.uuid4().hex[:8]


def _replace_item_custom(version, rejected_key, label, is_core):
    """User tapped "swap" → "Write my own" and typed `label`. Substitute it in
    place of `rejected_key` — a brand-new item (fresh key), applied to the
    CURRENT version instantly. Returns {key,label,core} or None."""
    label = (label or "").strip()[:60]
    if not label:
        return None
    item = {"key": _new_user_key(), "label": label}
    if not is_core:
        # The beta dashboard and set_item_state only accept health-tagged
        # bonuses (health_bonus_items); a user-authored bonus is the user's
        # own choice, so tag it or it vanishes and its taps 400.
        item["category"] = "health"
    if _swap_or_append(version, item, rejected_key, is_core) is None:
        return None  # collision — astronomically unlikely with a fresh key
    return {**item, "core": is_core}


def _generate_core_item(participant, version, check_in):
    """Auto-suggest ONE new CORE item grounded in the user's data (for the
    "+ Add item → Auto suggest" path). Returns {key,label} or None."""
    from .services.ai_coach import generate_one_bonus

    g = _ai_grounding(participant, version, check_in.answers_by_key())
    return generate_one_bonus(
        participant_name=participant.display_name,
        attestation_text=g["att"],
        existing_items=g["existing"],
        today_done_labels=g["done"],
        today_comment=check_in.comment or "",
        rejected_labels=[],
        core=True,
    )


@require_daily_actor
@require_http_methods(["POST"])
def add_item(request):
    """Add a NEW core item to the current checklist, instantly. Body (JSON or
    form): mode = "custom" (use `label`) | "auto" (AI-suggested), plus an
    optional non-empty ``days`` list using Monday=0 through Sunday=6."""
    participant = request.daily_participant
    today = _resolve_today(request)
    if _is_backfill(request):
        return JsonResponse({"ok": False, "error": "no_add_in_backfill"}, status=400)

    body, err = _json_body(request)
    if err:
        return err
    mode = str(body.get("mode", "")).strip()
    label = str(body.get("label", "")).strip()
    raw_days = body.get("days") if "days" in body else None
    if raw_days is not None and not valid_habit_days(raw_days):
        return JsonResponse({"ok": False, "error": "bad_days"}, status=400)
    raw_step_rule = body.get("step_rule") if "step_rule" in body else None
    if raw_step_rule is not None and not valid_habit_step_rule(raw_step_rule):
        return JsonResponse({"ok": False, "error": "bad_step_rule"}, status=400)
    raw_steps = body.get("steps", [])
    if not isinstance(raw_steps, list) or len(raw_steps) > MAX_CHECKLIST_SIZE:
        return JsonResponse({"ok": False, "error": "bad_steps"}, status=400)
    step_labels = []
    for step in raw_steps:
        if not isinstance(step, str):
            return JsonResponse({"ok": False, "error": "bad_steps"}, status=400)
        step = step.strip()[:60]
        if step:
            step_labels.append(step)

    version = participant.get_or_create_current_checklist()
    if len(version.questions) >= MAX_CHECKLIST_SIZE:
        return JsonResponse(
            {"ok": False, "error": "at_max", "max": MAX_CHECKLIST_SIZE}, status=409
        )
    check_in = _get_or_create_today(participant, today, version)

    if mode == "custom":
        label = label[:60]
        if not label:
            return JsonResponse({"ok": False, "error": "empty_label"}, status=400)
        item = {"key": _new_user_key(), "label": label}
    else:
        item = _generate_core_item(participant, version, check_in)
        if item is None:
            return JsonResponse({"ok": False, "error": "coach_offline"}, status=503)

    if raw_days is not None:
        item = {**item, "days": sorted(raw_days)}
    if raw_step_rule is not None:
        item = {**item, "step_rule": raw_step_rule}
    if step_labels:
        item = {
            **item,
            "items": [
                {"key": _new_user_key(), "label": step_label}
                for step_label in step_labels
            ],
        }

    if _swap_or_append(version, item) is None:
        return JsonResponse({"ok": False, "error": "collision"}, status=409)
    # Seed a pending answer so the item is part of today's check-in right away.
    DailyCheckInAnswer.objects.bulk_create(
        [
            DailyCheckInAnswer(
                check_in=check_in,
                question_key=question_key,
                state=DailyCheckInAnswer.STATE_PENDING,
            )
            for question_key in [item["key"]] + [
                subitem["key"] for subitem in (item.get("items") or [])
            ]
        ],
        ignore_conflicts=True,
    )
    return JsonResponse({"ok": True, "item": _habit_json(item, today)})


@require_daily_actor
@require_http_methods(["POST"])
def edit_item(request):
    """Edit a core habit without changing its stable key, answer state, or
    nested detail items. Body: key, label, and optional weekday ``days``."""
    participant = request.daily_participant
    if _is_backfill(request):
        return JsonResponse({"ok": False, "error": "no_edit_in_backfill"}, status=400)

    body, err = _json_body(request)
    if err:
        return err
    key = str(body.get("key", "")).strip()
    label = str(body.get("label", "")).strip()[:60]
    if not label:
        return JsonResponse({"ok": False, "error": "empty_label"}, status=400)
    raw_days = body.get("days") if "days" in body else None
    if raw_days is not None and not valid_habit_days(raw_days):
        return JsonResponse({"ok": False, "error": "bad_days"}, status=400)
    raw_step_rule = body.get("step_rule") if "step_rule" in body else None
    if raw_step_rule is not None and not valid_habit_step_rule(raw_step_rule):
        return JsonResponse({"ok": False, "error": "bad_step_rule"}, status=400)

    version = participant.get_or_create_current_checklist()
    with transaction.atomic():
        current = ChecklistVersion.objects.select_for_update().get(id=version.id)
        if not any(q["key"] == key for q in current.questions):
            return JsonResponse({"ok": False, "error": "bad_key"}, status=400)
        questions = []
        updated = None
        for question in current.questions:
            if question["key"] == key:
                updated = {**question, "label": label}
                if raw_days is not None:
                    updated["days"] = sorted(raw_days)
                if raw_step_rule is not None:
                    updated["step_rule"] = raw_step_rule
                question = updated
            questions.append(question)
        current.questions = questions
        current.save(update_fields=["questions"])

    check_in = DailyCheckIn.objects.filter(
        participant=participant, date=_resolve_today(request)
    ).first()
    states = check_in.answers_by_key() if check_in else {}
    if check_in and raw_step_rule is not None and updated.get("items"):
        _derive_parent(check_in, updated, states)
        refresh_streak_cache(
            participant,
            today=_real_today(request),
            changed_check_in=check_in,
            done_count=sum(
                1 for answer_state in states.values()
                if answer_state == DailyCheckInAnswer.STATE_DONE
            ),
        )
    return JsonResponse({
        "ok": True,
        "item": _habit_json(updated, _resolve_today(request), states),
    })


@require_daily_actor
@require_http_methods(["POST"])
def remove_item(request):
    """Remove a core habit and its detail items from the current checklist.
    Today's answers for the removed keys are discarded; older answer rows are
    kept as historical data. Body (JSON or form): key."""
    participant = request.daily_participant
    today = _resolve_today(request)
    if _is_backfill(request):
        return JsonResponse({"ok": False, "error": "no_edit_in_backfill"}, status=400)

    body, err = _json_body(request)
    if err:
        return err
    key = str(body.get("key", "")).strip()

    version = participant.get_or_create_current_checklist()
    check_in = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    with transaction.atomic():
        current = ChecklistVersion.objects.select_for_update().get(id=version.id)
        item = next((q for q in current.questions if q["key"] == key), None)
        if item is None:
            return JsonResponse({"ok": False, "error": "bad_key"}, status=400)
        removed_keys = [key] + [s["key"] for s in (item.get("items") or [])]
        current.questions = [q for q in current.questions if q["key"] != key]
        current.save(update_fields=["questions"])
        DailyCheckInAnswer.objects.filter(
            check_in__participant=participant,
            check_in__date=today,
            question_key__in=removed_keys,
        ).delete()

    if check_in is not None:
        refresh_streak_cache(
            participant,
            today=_real_today(request),
            changed_check_in=check_in,
        )

    return JsonResponse({"ok": True, "removed_key": key})


@require_daily_actor
@require_http_methods(["POST"])
def add_subitem(request):
    """Add one or more detail steps under a core habit.

    Body: ``parent_key`` plus legacy ``label`` or a batch ``labels`` list. All
    steps are appended in one locked checklist update and returned together.
    """
    participant = request.daily_participant
    today = _resolve_today(request)
    if _is_backfill(request):
        return JsonResponse({"ok": False, "error": "no_add_in_backfill"}, status=400)

    body, err = _json_body(request)
    if err:
        return err
    parent_key = str(body.get("parent_key", "")).strip()
    raw_labels = body.get("labels") if "labels" in body else [body.get("label", "")]
    if not isinstance(raw_labels, list) or len(raw_labels) > MAX_CHECKLIST_SIZE:
        return JsonResponse({"ok": False, "error": "bad_labels"}, status=400)
    labels = []
    for label in raw_labels:
        if not isinstance(label, str):
            return JsonResponse({"ok": False, "error": "bad_labels"}, status=400)
        label = label.strip()[:60]
        if label:
            labels.append(label)
    if not labels:
        return JsonResponse({"ok": False, "error": "empty_label"}, status=400)

    version = participant.get_or_create_current_checklist()
    if not any(q["key"] == parent_key for q in version.questions):
        return JsonResponse({"ok": False, "error": "bad_parent"}, status=400)
    check_in = _get_or_create_today(participant, today, version)

    items = [{"key": _new_user_key(), "label": label} for label in labels]
    with transaction.atomic():
        v = ChecklistVersion.objects.select_for_update().get(id=version.id)
        item_keys = {item["key"] for item in items}
        if len(item_keys) != len(items) or item_keys & all_version_keys(v):
            return JsonResponse({"ok": False, "error": "collision"}, status=409)
        questions = []
        for q in v.questions:
            if q["key"] == parent_key:
                q = {**q, "items": list(q.get("items") or []) + items}
            questions.append(q)
        v.questions = questions
        v.save(update_fields=["questions"])
        DailyCheckInAnswer.objects.bulk_create(
            [
                DailyCheckInAnswer(
                    check_in=check_in,
                    question_key=item["key"],
                    state=DailyCheckInAnswer.STATE_PENDING,
                )
                for item in items
            ],
            ignore_conflicts=True,
        )
    states = check_in.answers_by_key()
    parent = next(q for q in questions if q["key"] == parent_key)
    return JsonResponse({
        "ok": True,
        "parent_key": parent_key,
        "item": items[0],
        "items": items,
        "habit": _habit_json(parent, today, states),
    })


@require_daily_actor
@require_http_methods(["POST"])
def remove_subitem(request):
    """Remove a detail sub-item from its parent habit. Deletes it from the
    version's `items` and drops today's answer, then re-derives the parent's
    done state from whatever sub-items remain."""
    participant = request.daily_participant
    today = _resolve_today(request)
    if _is_backfill(request):
        return JsonResponse({"ok": False, "error": "no_edit_in_backfill"}, status=400)

    body, err = _json_body(request)
    if err:
        return err
    parent_key = str(body.get("parent_key", "")).strip()
    key = str(body.get("key", "")).strip()

    version = participant.get_or_create_current_checklist()
    parent_q = next((q for q in version.questions if q["key"] == parent_key), None)
    if parent_q is None or not any(s["key"] == key for s in (parent_q.get("items") or [])):
        return JsonResponse({"ok": False, "error": "bad_key"}, status=400)
    check_in = _get_or_create_today(participant, today, version)

    with transaction.atomic():
        v = ChecklistVersion.objects.select_for_update().get(id=version.id)
        remaining_subs = []
        questions = []
        for q in v.questions:
            if q["key"] == parent_key:
                remaining_subs = [s for s in (q.get("items") or []) if s["key"] != key]
                q = {**q, "items": remaining_subs}
            questions.append(q)
        v.questions = questions
        v.save(update_fields=["questions"])
        DailyCheckInAnswer.objects.filter(check_in=check_in, question_key=key).delete()
        states = check_in.answers_by_key()
        parent = next(q for q in questions if q["key"] == parent_key)
        parent_update = _derive_parent(check_in, parent, states)
    refresh_streak_cache(
        participant,
        today=_real_today(request),
        changed_check_in=check_in,
        done_count=sum(
            1 for answer_state in states.values()
            if answer_state == DailyCheckInAnswer.STATE_DONE
        ),
    )
    parent = next(q for q in questions if q["key"] == parent_key)
    return JsonResponse({
        "ok": True,
        "parent": parent_update,
        "habit": _habit_json(parent, today, states),
    })


# ===== Wins backlog (beta) ================================================
# The positive face of a put-off thing. A pile that is never rendered all at
# once or counted; the user selects a single "today's win." Every endpoint
# scopes to request.daily_participant (single-user authorization invariant,
# plan section 8a) — the win id from the client is validated against the
# participant's OWN wins, so a foreign id 404s.


def _win_json(win):
    """Serialize a surfaced win for the crown. `goal` is the "part of: ..." line:
    the north star's text when the win is a stepping stone."""
    if win is None:
        return None
    goal = win.parent if win.parent_id else None
    goal_can_complete = bool(
        goal
        and goal.status == WinItem.STATUS_OPEN
        and win.status != WinItem.STATUS_OPEN
        and not goal.stones.filter(status=WinItem.STATUS_OPEN).exists()
    )
    return {
        "id": win.id,
        "title": win.text,
        "goal": goal.text if goal else "",
        "goal_id": goal.id if goal else None,
        "goal_can_complete": goal_can_complete,
    }


def _wins_state_json(participant, today):
    """The complete daily-card state returned by every wins mutation."""
    from .services.wins import (
        get_completed_todays_win,
        get_todays_win,
        select_next_todays_win,
    )

    completed_today = get_completed_todays_win(participant, today)
    next_win = get_todays_win(participant, today)
    if next_win is None and completed_today is None:
        next_win = select_next_todays_win(participant, today)
    return {
        "next": _win_json(next_win),
        "completed_today": _win_json(completed_today),
        "today_has_completed_win": completed_today is not None,
    }


@require_daily_actor
@require_http_methods(["POST"])
def win_add(request):
    """Add a win to the backlog, filling an empty daily card. Beta-only."""
    from .services.wins import add_win

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    today = _resolve_today(request)

    body, err = _json_body(request)
    if err:
        return err
    text = str(body.get("text", "")).strip()

    win = add_win(participant, text)
    if win is None:
        return JsonResponse({"ok": False, "error": "empty_or_full"}, status=400)
    return JsonResponse({
        "ok": True,
        "added": _stone_json(win, today),
        **_wins_state_json(participant, today),
    })


@require_daily_actor
@require_http_methods(["POST"])
def win_select(request):
    """Let the user explicitly choose the leaf shown as Today's Win."""
    from .services.wins import select_todays_win

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    body, err = _json_body(request)
    if err:
        return err
    try:
        win = participant.wins.get(
            id=body.get("id"), status=WinItem.STATUS_OPEN, is_goal=False
        )
    except (WinItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    today = _resolve_today(request)
    selected = select_todays_win(participant, win, today)
    return JsonResponse({
        "ok": True,
        "selected": _stone_json(selected, today),
        **_wins_state_json(participant, today),
    })


@require_daily_actor
@require_http_methods(["POST"])
def win_action(request):
    """Check off, uncheck, or defer a participant-owned win leaf."""
    from .services.wins import complete_win, defer_win, uncomplete_win

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    today = _resolve_today(request)

    body, err = _json_body(request)
    if err:
        return err
    win_id = body.get("id")
    action = str(body.get("action", "")).strip()

    try:
        if action == "uncheck":
            # Undo is available for a checked one-off win, or a checked step
            # whose North Star is still in Working toward. Graduated habit
            # steps and steps under an achieved North Star are not undone.
            win = (
                participant.wins.select_related("parent")
                .filter(
                    Q(parent__isnull=True)
                    | Q(parent__is_goal=True, parent__status=WinItem.STATUS_OPEN)
                )
                .get(id=win_id, is_goal=False, status=WinItem.STATUS_DONE)
            )
        else:
            # is_goal=False: only leaves (standalone wins / stepping stones)
            # can be acted on. A North Star has its own completion endpoint.
            win = participant.wins.get(
                id=win_id, is_goal=False, status=WinItem.STATUS_OPEN
            )
    except (WinItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)

    if action == "did_it":
        # The daily-card action is valid only for the leaf selected today
        # (automatically or by the user). This is the sole path that earns the
        # week-strip star.
        if win.surfaced_on != today:
            return JsonResponse({"ok": False, "error": "not_selected"}, status=409)
        completed, _ = complete_win(win, featured_on=today)
    elif action == "check_off":
        # The editor may check any north-star step. If it is also today's
        # daily selection, preserve the same daily-win semantics.
        featured_on = today if win.surfaced_on == today else None
        completed, _ = complete_win(win, featured_on=featured_on)
    elif action == "uncheck":
        reopened = uncomplete_win(win, today)
    elif action == "not_today":
        if win.surfaced_on != today:
            return JsonResponse({"ok": False, "error": "not_selected"}, status=409)
        defer_win(win, today)
    else:
        return JsonResponse({"ok": False, "error": "bad_action"}, status=400)

    payload = {"ok": True, **_wins_state_json(participant, today)}
    if action in {"did_it", "check_off"}:
        payload["completed"] = _stone_json(completed, today)
        payload["featured_done"] = completed.surfaced_on == today
    elif action == "uncheck":
        payload["reopened"] = _stone_json(reopened, today)
    return JsonResponse(payload)


# ===== Wins editor: the "your list" door (beta) ==========================
# The ONE place the full pile is shown — north stars with their stepping stones
# and a count, plus one-off wins. The daily surface never advertises size; this
# page does. All endpoints scope to request.daily_participant.


def _stone_json(win, today=None):
    return {
        "id": win.id,
        "text": win.text,
        "status": win.status,
        "done": win.status != WinItem.STATUS_OPEN,
        "selected_today": bool(today and win.surfaced_on == today),
    }


@require_daily_actor
@require_http_methods(["GET"])
def habits_edit(request):
    """Manage the beta participant's full recurring habit list and schedule."""
    participant = request.daily_participant
    if not _is_beta(request, participant):
        return redirect("daily:checkin")

    today = _resolve_today(request)
    version = participant.get_or_create_current_checklist()
    habits_dialog = request.GET.get("fragment") == "1"
    context = {
        "participant": participant,
        "habits": [_habit_json(question, today) for question in version.questions],
        "max_checklist_size": MAX_CHECKLIST_SIZE,
        "habits_dialog": habits_dialog,
        "self_token": "" if habits_dialog else str(_active_token(participant) or ""),
        "theme": _resolve_theme(request),
        "beta_toggle": settings.DEBUG,
        "dev_gate": settings.DEBUG and request.session.get("daily_gate_forced", False),
    }
    template = "daily/_habits_editor.html" if habits_dialog else "daily/habits_edit.html"
    return render(request, template, context)


@require_daily_actor
@require_http_methods(["GET"])
def wins_edit(request):
    """The 'your list' editor page: manage north stars, their stepping stones,
    and one-off wins. Beta-only."""
    from .services.wins import list_backlog

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return redirect("daily:checkin")

    today = _resolve_today(request)
    backlog = list_backlog(participant)
    wins_dialog = request.GET.get("fragment") == "1"
    context = {
        "participant": participant,
        "goals": backlog["goals"],   # [{"goal": WinItem, "stones": [WinItem, ...]}]
        "singles": backlog["singles"],
        "today": today,
        "has_achieved": backlog["has_achieved"],
        "has_archived": backlog["has_archived"],
        "wins_dialog": wins_dialog,
        "self_token": "" if wins_dialog else str(_active_token(participant) or ""),
        "theme": _resolve_theme(request),
        # Same gate controls the check-in uses, so the install/notification gate
        # auto-skips in DEV here too (otherwise base.html traps this page behind
        # the "Add to Home Screen" front door).
        "beta_toggle": settings.DEBUG,
        "dev_gate": settings.DEBUG and request.session.get("daily_gate_forced", False),
    }
    template = "daily/_wins_editor.html" if wins_dialog else "daily/wins_edit.html"
    return render(request, template, context)


@require_daily_actor
@require_http_methods(["GET"])
def win_candidates(request):
    """Feed for the "Pick one" sheet: every open leaf that could become
    Today's Win, without a trip through the full editor. North Star steps come
    first (grouped under their goal, in editor order), then one-off wins."""
    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    today = _resolve_today(request)
    leaves = list(
        participant.wins.filter(is_goal=False, status=WinItem.STATUS_OPEN)
        .filter(
            Q(parent__isnull=True)
            | Q(parent__is_goal=True, parent__status=WinItem.STATUS_OPEN)
        )
        .select_related("parent")
        .order_by("order", "created_at")
    )
    steps = [w for w in leaves if w.parent_id]
    steps.sort(key=lambda w: (w.parent.created_at, w.order, w.created_at))
    singles = [w for w in leaves if not w.parent_id]
    return JsonResponse({
        "ok": True,
        "candidates": [
            {
                "id": w.id,
                "text": w.text,
                "goal": w.parent.text if w.parent_id else "",
                "selected_today": w.surfaced_on == today,
            }
            for w in steps + singles
        ],
    })


@require_daily_actor
@require_http_methods(["GET"])
def wins_achieved(request):
    """Finished north stars with the full ladder of steps that got each one
    done (done stones plus any that graduated into habits). Read-only legacy
    route; the combined Archived view is the primary history entry point."""
    participant = request.daily_participant
    if not _is_beta(request, participant):
        return redirect("daily:checkin")

    goals = list(
        participant.wins.filter(
            is_goal=True, status=WinItem.STATUS_DONE
        ).order_by("-done_at", "-created_at")[:50]
    )
    stones_by_goal = {}
    for s in participant.wins.filter(parent__in=goals).order_by("order", "created_at"):
        stones_by_goal.setdefault(s.parent_id, []).append(s)
    context = {
        "participant": participant,
        "achieved": [
            {"goal": g, "stones": stones_by_goal.get(g.id, [])} for g in goals
        ],
        "theme": _resolve_theme(request),
        "beta_toggle": settings.DEBUG,
        "dev_gate": settings.DEBUG and request.session.get("daily_gate_forced", False),
    }
    return render(request, "daily/wins_achieved.html", context)


@require_daily_actor
@require_http_methods(["GET"])
def wins_archived(request):
    """North Star history: achieved work plus restorable archived work."""
    participant = request.daily_participant
    if not _is_beta(request, participant):
        return redirect("daily:checkin")

    goals = list(
        participant.wins.filter(
            is_goal=True,
            status__in=[WinItem.STATUS_DONE, WinItem.STATUS_ARCHIVED],
        ).order_by("-created_at")[:100]
    )
    achieved_goals = sorted(
        (goal for goal in goals if goal.status == WinItem.STATUS_DONE),
        key=lambda goal: goal.done_at or goal.created_at,
        reverse=True,
    )[:50]
    archived_goals = [
        goal for goal in goals if goal.status == WinItem.STATUS_ARCHIVED
    ][:50]
    stones_by_goal = {}
    for stone in participant.wins.filter(parent__in=goals).order_by("order", "created_at"):
        stones_by_goal.setdefault(stone.parent_id, []).append(stone)
    wins_dialog = request.GET.get("fragment") == "1"
    context = {
        "participant": participant,
        "achieved": [
            {"goal": goal, "stones": stones_by_goal.get(goal.id, [])}
            for goal in achieved_goals
        ],
        "archived": [
            {"goal": goal, "stones": stones_by_goal.get(goal.id, [])}
            for goal in archived_goals
        ],
        "wins_dialog": wins_dialog,
        "theme": _resolve_theme(request),
        "beta_toggle": settings.DEBUG,
        "dev_gate": settings.DEBUG and request.session.get("daily_gate_forced", False),
    }
    template = "daily/_wins_archived_panel.html" if wins_dialog else "daily/wins_archived.html"
    return render(request, template, context)


def _json_body(request):
    """Parse a POST body that may be JSON or form-encoded. Returns
    (body, error_response): body is a dict or QueryDict (both support .get),
    error_response is a ready 400 on malformed JSON (else None)."""
    if request.content_type == "application/json":
        try:
            return json.loads(request.body or b"{}"), None
        except json.JSONDecodeError:
            return None, JsonResponse({"ok": False, "error": "bad_json"}, status=400)
    return request.POST, None


@require_daily_actor
@require_http_methods(["POST"])
def win_goal_add(request):
    """Create a north star with an initial ladder of stepping stones. Beta-only.
    Body: {goal: str, stones: [str, ...]}."""
    from .services.wins import create_north_star

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    body, err = _json_body(request)
    if err:
        return err
    goal_text = str(body.get("goal", "")).strip()
    stones = body.get("stones") or []
    if isinstance(stones, str):
        stones = [stones]
    stones = [str(s) for s in stones]
    goal = create_north_star(participant, goal_text, stones)
    if goal is None:
        return JsonResponse({"ok": False, "error": "empty_or_full"}, status=400)
    today = _resolve_today(request)
    return JsonResponse({
        "ok": True,
        **_wins_state_json(participant, today),
        "goal": {
            "id": goal.id,
            "text": goal.text,
            "stones": [
                _stone_json(s, today)
                for s in goal.stones.order_by("order", "created_at")
            ],
        },
    })


@require_daily_actor
@require_http_methods(["POST"])
def win_goal_edit(request):
    """Rename an active, participant-owned North Star or one-off win (both
    are parentless rows). Beta-only. Body: {id: int, text: str}."""
    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    body, err = _json_body(request)
    if err:
        return err
    text = str(body.get("text", "")).strip()
    if not text or len(text) > WinItem._meta.get_field("text").max_length:
        return JsonResponse({"ok": False, "error": "bad_text"}, status=400)
    try:
        goal = participant.wins.get(
            id=body.get("id"), parent__isnull=True, status=WinItem.STATUS_OPEN
        )
    except (WinItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    goal.text = text
    goal.save(update_fields=["text"])
    today = _resolve_today(request)
    return JsonResponse({
        "ok": True,
        "goal": {"id": goal.id, "text": goal.text},
        **_wins_state_json(participant, today),
    })


@require_daily_actor
@require_http_methods(["POST"])
def win_stone_add(request):
    """Append a stepping stone to a north star. Pointing it at a standalone
    one-off win first grows that win into a north star (its text becomes the
    goal, this step becomes the first stone). Beta-only.
    Body: {goal_id: int, text: str}."""
    from .services.wins import add_stone, convert_to_north_star

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    body, err = _json_body(request)
    if err:
        return err
    try:
        goal = participant.wins.filter(parent__isnull=True).filter(
            Q(is_goal=True) | Q(status=WinItem.STATUS_OPEN)
        ).get(id=body.get("goal_id"))
    except (WinItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    text = str(body.get("text", ""))
    converted = False
    if not goal.is_goal:
        # Validate before converting, so an empty step never leaves behind a
        # stoneless north star.
        if not text.strip():
            return JsonResponse({"ok": False, "error": "empty_or_full"}, status=400)
        goal = convert_to_north_star(goal)
        converted = True
    stone = add_stone(participant, goal, text)
    if stone is None:
        return JsonResponse({"ok": False, "error": "empty_or_full"}, status=400)
    today = _resolve_today(request)
    payload = {
        "ok": True,
        "stone": _stone_json(stone, today),
        **_wins_state_json(participant, today),
    }
    if converted:
        payload["converted"] = True
        payload["goal"] = {"id": goal.id, "text": goal.text}
    return JsonResponse(payload)


@require_daily_actor
@require_http_methods(["POST"])
def win_goal_complete(request):
    """Complete a north star after every one of its steps is checked."""
    from .services.wins import complete_goal

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    body, err = _json_body(request)
    if err:
        return err
    try:
        goal = participant.wins.get(
            id=body.get("id"), is_goal=True, status=WinItem.STATUS_OPEN
        )
    except (WinItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    completed = complete_goal(goal)
    if completed is None:
        return JsonResponse({"ok": False, "error": "steps_remaining"}, status=409)
    today = _resolve_today(request)
    return JsonResponse({
        "ok": True,
        "north_star_done": {"id": completed.id, "title": completed.text},
        **_wins_state_json(participant, today),
    })


@require_daily_actor
@require_http_methods(["POST"])
def win_goal_archive(request):
    """Soft-delete an active North Star by moving it to Archived."""
    from .services.wins import archive_goal

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    body, err = _json_body(request)
    if err:
        return err
    try:
        goal = participant.wins.get(
            id=body.get("id"), is_goal=True, status=WinItem.STATUS_OPEN
        )
    except (WinItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    archived = archive_goal(goal)
    today = _resolve_today(request)
    return JsonResponse({
        "ok": True,
        "archived": {"id": archived.id, "title": archived.text},
        **_wins_state_json(participant, today),
    })


@require_daily_actor
@require_http_methods(["POST"])
def win_goal_restore(request):
    """Move a participant-owned history item back to Working toward."""
    from .services.wins import restore_goal

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    body, err = _json_body(request)
    if err:
        return err
    # The no-JS archived page submits one form per card with a checkbox per
    # goal, so a native POST may carry several ids. The dialog posts JSON with
    # a single id per request.
    raw_ids = body.getlist("id") if hasattr(body, "getlist") else [body.get("id")]
    try:
        goals = [
            participant.wins.get(
                id=raw_id,
                is_goal=True,
                status__in=[WinItem.STATUS_DONE, WinItem.STATUS_ARCHIVED],
            )
            for raw_id in raw_ids
        ]
    except (WinItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    restored_goals = [restore_goal(goal) for goal in goals]
    if request.content_type != "application/json":
        # Covers the nothing-checked native submit too: just show the page.
        return redirect("daily:wins_archived")
    if not restored_goals:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    restored = restored_goals[0]
    today = _resolve_today(request)
    return JsonResponse({
        "ok": True,
        "goal": {
            "id": restored.id,
            "text": restored.text,
            "stones": [
                _stone_json(stone, today)
                for stone in restored.stones.order_by("order", "created_at")
            ],
        },
        **_wins_state_json(participant, today),
    })


@require_daily_actor
@require_http_methods(["POST"])
def win_remove(request):
    """Delete a win — a one-off, a stone, or a whole north star (its stones
    cascade). Beta-only. The id is validated against the participant's pile."""
    from .services.wins import remove_win

    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    body, err = _json_body(request)
    if err:
        return err
    try:
        win = participant.wins.get(id=body.get("id"))
    except (WinItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    remove_win(win)
    today = _resolve_today(request)
    return JsonResponse({"ok": True, **_wins_state_json(participant, today)})


def _generate_and_append_bonus(
    participant, version, check_in, states, health_only=False,
):
    """Generate one fresh bonus item (live AI), append to the current
    version's bonus_questions, return {key,label} or None."""
    from .services.ai_coach import generate_one_bonus

    g = _ai_grounding(participant, version, states)
    item = generate_one_bonus(
        participant_name=participant.display_name,
        attestation_text=g["att"],
        existing_items=g["existing"],
        today_done_labels=g["done"],
        today_comment=check_in.comment or "",
        rejected_labels=g["skipped_bonus"],
        health_only=health_only,
    )
    if item is None:
        return None
    return _swap_or_append(version, item, is_core=False)


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
        reply = f'Day wrapped. Tomorrow is already planned: "{frog}" first. Rest up.'
    elif ready:
        reply = (
            "Day wrapped, your morning note is set. Want to set up tomorrow? "
            "What's the ONE thing you've been putting off that would matter most?"
        )
        invite_planning = True
    else:
        reply = (
            "I couldn't wrap the day just now (coach is offline). Your taps "
            "are saved. Tell me anything here and I'll fold it in overnight."
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
    beta_mode = _is_beta(request, participant)
    if _is_backfill(request):
        return JsonResponse({"ok": True, "new_bonus": None})
    version = participant.get_or_create_current_checklist()
    check_in = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    if check_in is None:
        return JsonResponse({"ok": True, "new_bonus": None})

    states = check_in.answers_by_key()
    core_questions = (
        scheduled_questions(version.questions, today)
        if beta_mode
        else version.questions
    )
    core_done = sum(
        1 for question in core_questions
        if states.get(question["key"]) == "done"
    )
    if core_done < BONUS_REVEAL_AT:
        return JsonResponse({"ok": True, "new_bonus": None})

    # Only top up if every existing bonus is resolved (done/skip) — keep
    # exactly one open bonus at a time so the pile grows by completion,
    # not by polling.
    visible_bonus_questions = (
        health_bonus_items(version.bonus_questions)
        if beta_mode
        else (version.bonus_questions or [])
    )
    open_bonus = [
        q for q in visible_bonus_questions
        if states.get(q["key"], "pending") == "pending"
    ]
    if open_bonus:
        return JsonResponse({"ok": True, "new_bonus": None})

    item = _generate_and_append_bonus(
        participant, version, check_in, states, health_only=beta_mode,
    )
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
        CoachChatMessage.objects.filter(participant=participant)
        # Older builds copied next-day reports into chat. Keep those rows for
        # audit/history, but do not render them as part of the conversation.
        .exclude(suggestion__check_in__date__lt=F("date"))
        .order_by("-created_at")[:limit]
    )
    msgs.reverse()
    return msgs


CHAT_HISTORY_PAGE_SIZE = 20


def _chat_history_page(participant, before_id=None):
    """Newest-first cursor query, returned chronologically for rendering."""
    from .models import CoachChatMessage

    query = (
        CoachChatMessage.objects.filter(participant=participant)
        .exclude(suggestion__check_in__date__lt=F("date"))
    )
    if before_id is not None:
        query = query.filter(id__lt=before_id)
    newest_first = list(query.order_by("-id")[:CHAT_HISTORY_PAGE_SIZE + 1])
    has_more = len(newest_first) > CHAT_HISTORY_PAGE_SIZE
    page = newest_first[:CHAT_HISTORY_PAGE_SIZE]
    page.reverse()
    return page, has_more, page[0].id if has_more and page else None


@require_daily_actor
@require_http_methods(["GET"])
def chat_history(request):
    """Return one 20-message page of the beta coach conversation.

    The newest page is prefetched only after the dashboard becomes idle. Older
    pages use ``?before=<oldest message id>`` as a participant-scoped cursor.
    Legacy still renders its conversation server-side.
    """
    participant = request.daily_participant
    before_id = request.GET.get("before")
    try:
        before_id = int(before_id) if before_id else None
        if before_id is not None and before_id < 1:
            raise ValueError
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "bad_cursor"}, status=400)

    messages, has_more, next_before = _chat_history_page(
        participant, before_id=before_id,
    )

    response = JsonResponse({
        "ok": True,
        "messages": [
            {
                "role": message.role,
                "text": message.text,
                "at": message.created_at.strftime("%-I:%M %p"),
            }
            for message in messages
        ],
        "has_more": has_more,
        "next_before": next_before,
    })
    response["Cache-Control"] = "private, no-store"
    return response


@require_daily_actor
@require_http_methods(["GET"])
def morning_report(request):
    """Deliver the newest prior-day Jamie report once, outside chat."""
    from .models import CoachChatMessage

    participant = request.daily_participant
    today = _resolve_today(request)
    if _is_backfill(request) or not _is_beta(request, participant):
        response = JsonResponse({"ok": True, "report": None})
        response["Cache-Control"] = "private, no-store"
        return response

    # Life-mode reports are deliberately generated off the dashboard's
    # critical path. If the hourly job missed, this separate request prepares
    # the report while Today remains fully usable.
    if not participant.ai_mutations_enabled:
        ensure_prior_day_coached(participant, today)

    note = _morning_note(participant, today)
    report = None
    if note and not CoachChatMessage.objects.filter(
        participant=participant,
        suggestion=note,
    ).exists():
        if note.status == CoachSuggestion.STATUS_PENDING:
            note.status = CoachSuggestion.STATUS_SHOWN
            note.save(update_fields=["status"])
        CoachChatMessage.objects.create(
            participant=participant,
            role=CoachChatMessage.ROLE_COACH,
            text=note.suggestion_text,
            date=today,
            suggestion=note,
        )
        report = {
            "id": note.id,
            "text": note.suggestion_text,
            "date": note.check_in.date.isoformat(),
        }

    response = JsonResponse({"ok": True, "report": report})
    response["Cache-Control"] = "private, no-store"
    return response


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
    from .services.ai_coach import (
        chat_reply, parse_planned_list, detect_crisis, CRISIS_RESPONSE,
    )
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

    # Safety: crisis/clinical signals short-circuit to a vetted, human-written
    # response and NEVER go to the model (plan section 3a). Applies to everyone.
    if detect_crisis(text):
        logger.warning("daily.chat: crisis pattern matched for %s — vetted reply", participant)
        coach_msg = CoachChatMessage.objects.create(
            participant=participant, role=CoachChatMessage.ROLE_COACH,
            text=CRISIS_RESPONSE, date=today,
        )
        return JsonResponse({
            "ok": True, "planned": False,
            "reply": {"text": coach_msg.text, "at": coach_msg.created_at.isoformat()},
        })

    # Beta support-only Jamie (mutations off) does not rewrite the list, so she
    # must not promise overnight changes and planning-mode queuing is disabled.
    is_beta = _is_beta(request, participant)
    mutations_on = (not is_beta) or participant.ai_mutations_enabled
    planning = planning and mutations_on
    chat_focus = participant.focus if is_beta else ""

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
        planning=planning, mutations_enabled=mutations_on, focus=chat_focus,
        beta=is_beta,
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
            suggestion, dropped = (None, [])
            if items is not None:
                # Legacy keeps the frozen replace-with-3 semantics; beta
                # merges the plan into the full user-curated list.
                suggestion, dropped = _queue_evening_plan(
                    participant, today, items, model, merge=is_beta,
                )
            if suggestion:
                planned = True
                if is_beta:
                    confirm = (
                        f'Locked in for tomorrow morning: "{items[0]["label"]}" '
                        "leads, and the rest of your list follows. You'll see it "
                        "when you open the app."
                    )
                    if dropped:
                        confirm += (
                            " (Your list is at its limit of "
                            f"{MAX_CHECKLIST_SIZE}, so I couldn't add "
                            + ", ".join(f'"{d}"' for d in dropped)
                            + ". Swap something out if you want it on there.)"
                        )
                else:
                    confirm = (
                        f'Locked in for tomorrow morning: "{items[0]["label"]}" '
                        "leads, then the other two. You'll see it when you open "
                        "the app."
                    )
                reply_text = f"{display}\n\n{confirm}".strip() if display else confirm
            elif items is not None and dropped:
                # The list is full and even the frog is new — be honest, don't
                # ask for a retry that can't succeed.
                full = (
                    f"(I couldn't lock that in: your list is already at "
                    f"{MAX_CHECKLIST_SIZE} items. Swap one out, or pick "
                    "tomorrow's lead from what's already on it.)"
                )
                reply_text = f"{display}\n\n{full}".strip() if display else full
            elif had_json:
                # The model tried to emit a plan but it didn't validate/queue.
                # Never show raw JSON, never claim success. Say so honestly.
                miss = "(I couldn't lock that in. Give me the three again and I'll retry.)"
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


def _norm_label(label):
    return " ".join(str(label or "").lower().split())


def _queue_evening_plan(participant, today, items, model_name="", merge=False):
    """Queue a user-authored evening plan as a pending CoachSuggestion on
    TODAY's check-in. The EXISTING auto-apply machinery (which only touches
    suggestions from days BEFORE "today") promotes it tomorrow morning; the
    overnight coach sees the pending plan and writes its note around it
    instead of competing (see coach_runner.run_coach).

    merge=False (legacy, FROZEN path): proposed_questions = exactly the 3
    planned items, replacing the 3-item list — the pre-beta behavior,
    unchanged.

    merge=True (beta): the planned items lead (frog first) and every OTHER
    current question follows in its existing order. Planned items are matched
    to existing questions by normalized label so the model re-inventing a key
    for "Morning walk" reorders that habit instead of duplicating it. The
    merged list must NEVER drop an existing habit: at the MAX_CHECKLIST_SIZE
    cap, excess NEW planned items are dropped instead (they were never on the
    list), and if even the lead win is new and can't fit, nothing is queued.
    Sub-items aren't carried here; apply_pending_mutations re-attaches them
    by key at apply time.

    Supersedes any other queued-but-unapplied mutation on today's check-in
    (an earlier plan tonight, a wrap-generated mutation) so exactly one
    mutation is ever pending — double-application can't happen.

    suggestion_text doubles as the fallback morning note if the overnight
    note-around-the-plan run never happens. Returns (suggestion, dropped):
    the created suggestion (or None) and the labels of planned items that
    didn't fit — (None, [frog_label]) means the list is full and the plan
    couldn't be honored at all.
    """
    if not items:
        return None, []
    version = participant.get_or_create_current_checklist()

    if not merge:
        proposed = list(items)
        dropped = []
    else:
        # Re-key planned items that name an existing habit (exact key or same
        # normalized label) so they reorder it rather than duplicate it, and
        # answer history / sub-item drawers follow the habit.
        existing_keys = {q["key"] for q in version.questions}
        key_by_label = {_norm_label(q["label"]): q["key"] for q in version.questions}
        planned, seen = [], set()
        for it in items:
            key = it["key"]
            if key not in existing_keys:
                key = key_by_label.get(_norm_label(it["label"]), key)
            if key in seen:
                continue
            seen.add(key)
            planned.append({"key": key, "label": it["label"]})
        rest = [
            {"key": q["key"], "label": q["label"]}
            for q in version.questions
            if q["key"] not in seen
        ]
        dropped = []
        overflow = len(planned) + len(rest) - MAX_CHECKLIST_SIZE
        if overflow > 0:
            # Shed NEW planned items (back first, frog last) — never an
            # existing habit. A planned item re-keyed to an existing habit
            # doesn't add length, so it always survives.
            for it in reversed(planned[1:] if planned else []):
                if overflow <= 0:
                    break
                if it["key"] not in existing_keys:
                    planned.remove(it)
                    dropped.append(it["label"])
                    overflow -= 1
            if overflow > 0:  # the frog itself is new and the list is full
                logger.warning(
                    "daily.plan: list full for %s; plan not queued", participant
                )
                return None, [items[0]["label"]]
        proposed = planned + rest
        if dropped:
            logger.info(
                "daily.plan: list full for %s; dropped new items %s",
                participant, dropped,
            )

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

    lead = items[0]["label"]
    if merge:
        note = (
            f'Your plan, set last night: "{lead}" comes first. That\'s your win '
            "for today, do it while you're fresh. The rest of your list is right "
            "behind it."
        )
    else:
        note = (
            f'Your plan, set last night: "{lead}" comes first. That\'s your win '
            "for today, do it while you're fresh. The other two are there to back "
            "it up."
        )
    return CoachSuggestion.objects.create(
        check_in=check_in,
        suggestion_text=note,
        proposed_questions=proposed,
        base_questions=[
            {"key": q["key"], "label": q["label"]} for q in version.questions
        ],
        rationale=CoachSuggestion.RATIONALE_EVENING_PLAN,
        status=CoachSuggestion.STATUS_PENDING,
        model_name=model_name or "",
    ), dropped


# --- PWA (installable home-screen app) -------------------------------------
# The check-in page is installable to the home screen so it lives in the
# user's daily phone ritual. These two endpoints are public (no participant
# session required): the browser fetches them outside the auth flow. The
# installed app's start_url is /daily/checkin/, which the user's existing
# daily session authenticates — no token in the manifest.

def manifest(request):
    """Web app manifest. Served from /daily/ so its scope covers the app.

    If a valid ?t=<token> is present (the manifest linked from a token page),
    start_url becomes that token URL. iOS captures start_url at "Add to Home
    Screen" time, so installing from a token page yields an icon that
    cold-launches authenticated inside the PWA's own cookie jar — the fix for
    iOS standalone-PWA cookie isolation (a tokenless start_url can't log in the
    installed app). The token is validated so a bogus ?t= can't poison it."""
    start_url = "/daily/checkin/"
    token = request.GET.get("t", "")
    if token:
        import uuid as _uuid
        from .models import DailyAccessToken
        try:
            _uuid.UUID(str(token))  # reject non-UUID ?t= before it hits the ORM
            valid = DailyAccessToken.objects.filter(
                token=token, revoked_at__isnull=True
            ).exists()
        except (ValueError, TypeError):
            valid = False
        if valid:
            start_url = f"/daily/c/{token}/"
    data = {
        "name": "Strong as an 0x — Daily",
        "short_name": "Daily",
        "description": "Your daily check-in. Fill the ring.",
        "start_url": start_url,
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
    questions = (
        scheduled_questions(version.questions, today)
        if participant.beta
        else version.questions
    )
    total = len(questions)
    ci = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    if ci is None:
        return total
    states = ci.answers_by_key()
    done = sum(
        1 for question in questions
        if states.get(question["key"]) == DailyCheckInAnswer.STATE_DONE
    )
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


@require_daily_actor
@require_http_methods(["POST"])
def submit_onboarding_beta(request):
    """Finish the BETA one-card onboarding: seed the ONE starter item the user
    chose (their own words, or an accepted suggestion), set their focus, and
    stamp onboarded_at. New beta users start near-empty (one card), NOT the
    legacy baseline-3 or the survey-seeded set. Beta-only.

    The focus answer ("What's this mostly for?") also picks their Jamie:
    health = the full coach (morning reports plus overnight tune-ups), while
    life = a cheerleader report with no automatic list changes.
    The overnight prompt's health/fitness scope gate stays as the runtime
    backstop either way."""
    participant = request.daily_participant
    if not _is_beta(request, participant):
        return JsonResponse({"ok": False, "error": "not_beta"}, status=403)
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    # Double-submit / already-onboarded guard: never overwrite an existing list.
    if not _needs_onboarding(participant):
        return JsonResponse({"ok": True, "redirect": "/daily/checkin/"})

    focus = str(body.get("focus", "")).strip()
    if focus not in (DailyParticipant.FOCUS_HEALTH, DailyParticipant.FOCUS_LIFE):
        focus = ""
    label = str(body.get("label", "")).strip()[:60]
    if not label:
        return JsonResponse({"ok": False, "error": "empty_label"}, status=400)

    item = {"key": _new_user_key(), "label": label}
    with transaction.atomic():
        participant.checklist_versions.filter(is_current=True).update(is_current=False)
        ChecklistVersion.objects.create(
            participant=participant, questions=[item],
            source=ChecklistVersion.SOURCE_BASELINE, is_current=True,
        )
        participant.focus = focus
        participant.ai_mutations_enabled = focus == DailyParticipant.FOCUS_HEALTH
        participant.onboarded_at = timezone.now()
        if not participant.source:
            participant.source = "onboarding"
        participant.save(update_fields=[
            "focus", "ai_mutations_enabled", "onboarded_at", "source", "updated_at",
        ])
    if not participant.ai_mutations_enabled:
        dismiss_pending_mutations(participant)
    return JsonResponse({"ok": True, "redirect": "/daily/checkin/"})
