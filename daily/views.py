"""
Daily checklist views.

Endpoints:
  - token_login: /daily/c/<uuid>/ → set session, redirect to checkin
  - checkin: GET / POST. After submit, today is locked and tomorrow's
             preview is shown using the same checkbox UI (disabled).
             Reopen-for-edit via ?edit=1.
  - tomorrow_preview_json: poll target for the live tomorrow preview
  - modify_tomorrow: free-text refine of the proposed tomorrow checklist
  - respond_to_suggestion: dismiss / undo
  - reset_to_baseline_view: escape hatch

Dev-only (DEBUG=True): ?as_of=YYYY-MM-DD overrides "today".
"""
import logging
import threading
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
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


def _as_of_query(request, extra=None) -> str:
    parts = []
    if settings.DEBUG:
        val = request.GET.get("as_of")
        if val:
            parts.append(f"as_of={val}")
    if extra:
        parts.append(extra)
    return ("?" + "&".join(parts)) if parts else ""


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


def _latest_suggestion_for_day(participant: DailyParticipant, day: date):
    """Suggestion attached to the check-in for `day` (if any). Used to
    render tomorrow's preview when we're sitting on a check-in for the
    day BEFORE.
    """
    ci = DailyCheckIn.objects.filter(participant=participant, date=day).first()
    if not ci:
        return None
    return (
        ci.suggestions
        .exclude(status=CoachSuggestion.STATUS_DISMISSED)
        .order_by("-created_at")
        .first()
    )


def _shown_suggestion_for_today(participant: DailyParticipant, today: date):
    """The 'today's note' banner: most recent non-dismissed suggestion
    from yesterday's check-in. (Renamed conceptually from "yesterday's
    note" to "today's note" — the suggestion was written by the AI to
    be read on the day it auto-applies.)
    """
    return (
        CoachSuggestion.objects.filter(
            check_in__participant=participant,
            check_in__date__lt=today,
        )
        .exclude(status=CoachSuggestion.STATUS_DISMISSED)
        .order_by("-created_at")
        .first()
    )


@require_daily_actor
@require_http_methods(["GET", "POST"])
def checkin(request):
    participant: DailyParticipant = request.daily_participant
    today = _resolve_today(request)

    # Auto-apply any pending mutations BEFORE resolving the active version,
    # so today's render reflects what the AI proposed last night.
    applied = apply_pending_mutations(participant, as_of=today)
    if applied:
        logger.info("daily.checkin: applied %d pending mutations for %s", applied, participant)

    current_version = participant.get_or_create_current_checklist()
    existing = DailyCheckIn.objects.filter(participant=participant, date=today).first()

    if request.method == "POST":
        with transaction.atomic():
            check_in, created = DailyCheckIn.objects.update_or_create(
                participant=participant,
                date=today,
                defaults={
                    "checklist_version": current_version,
                    "comment": request.POST.get("comment", "").strip(),
                    "source": DailyCheckIn.SOURCE_WEB,
                },
            )
            check_in.answers.all().delete()
            DailyCheckInAnswer.objects.bulk_create([
                DailyCheckInAnswer(
                    check_in=check_in,
                    question_key=q["key"],
                    value=_checkbox(request, q["key"]),
                )
                for q in current_version.questions
            ])

        # Run the coach synchronously. A background daemon thread does NOT
        # survive reliably under Apache mod_wsgi (the worker finishes the
        # response and the thread can be killed before it writes the
        # suggestion). Synchronous is slower (~3-5s) but always works; the
        # client-side confetti animation covers the wait.
        _run_coach(check_in.id)

        # JS path: return JSON with the rendered locked-summary HTML so the
        # client can swap it in-place instead of triggering a full reload.
        if "application/json" in request.headers.get("Accept", ""):
            today_questions_rendered = [
                {
                    "key": q["key"],
                    "label": q["label"],
                    "checked": _checkbox(request, q["key"]),
                }
                for q in current_version.questions
            ]
            locked_html = render_to_string(
                "daily/_locked_summary.html",
                {
                    "questions": today_questions_rendered,
                    "existing": check_in,
                    "debug_as_of": request.GET.get("as_of") if settings.DEBUG else None,
                },
                request=request,
            )
            return JsonResponse({
                "ok": True,
                "locked_html": locked_html,
                "tomorrow_date_human": (today + timedelta(days=1)).strftime("%A, %B ") + str((today + timedelta(days=1)).day),
            })

        # No-JS path: classic redirect.
        return redirect(f"/daily/checkin/{_as_of_query(request)}")

    edit_mode = request.GET.get("edit") == "1"
    locked = existing is not None and not edit_mode

    todays_note = _shown_suggestion_for_today(participant, today)
    if todays_note and todays_note.status == CoachSuggestion.STATUS_PENDING:
        todays_note.status = CoachSuggestion.STATUS_SHOWN
        todays_note.save(update_fields=["status"])

    today_questions = [
        {"key": q["key"], "label": q["label"], "checked": False}
        for q in current_version.questions
    ]
    if existing:
        answers_map = existing.answers_by_key()
        for q in today_questions:
            q["checked"] = answers_map.get(q["key"], False)

    tomorrow_preview = None
    if locked:
        tomorrow_suggestion = _latest_suggestion_for_day(participant, today)
        if tomorrow_suggestion and tomorrow_suggestion.proposed_questions:
            tomorrow_preview = {
                "questions": tomorrow_suggestion.proposed_questions,
                "note": tomorrow_suggestion.suggestion_text,
                "suggestion_id": tomorrow_suggestion.id,
            }

    context = {
        "participant": participant,
        "today": today,
        "tomorrow": today + timedelta(days=1),
        "existing": existing,
        "locked": locked,
        "edit_mode": edit_mode,
        "todays_note": todays_note,
        "questions": today_questions,
        "tomorrow_preview": tomorrow_preview,
        "version": current_version,
        "is_baseline": _is_baseline_questions(current_version.questions),
        "debug_as_of": request.GET.get("as_of") if settings.DEBUG else None,
        "tomorrow_qs": _as_of_query_for_date(today + timedelta(days=1)) if settings.DEBUG else "",
        "yesterday_qs": _as_of_query_for_date(today - timedelta(days=1)) if settings.DEBUG else "",
        "today_qs": "",
        "today_iso": today.isoformat(),
    }
    return render(request, "daily/checkin.html", context)


def _is_baseline_questions(qs):
    if len(qs) != len(BASELINE_QUESTIONS):
        return False
    return all(
        a.get("key") == b.get("key") and a.get("label") == b.get("label")
        for a, b in zip(qs, BASELINE_QUESTIONS)
    )


def _as_of_query_for_date(d: date) -> str:
    return f"?as_of={d.isoformat()}"


def _checkbox(request, name):
    return request.POST.get(name) in ("on", "true", "1", "yes")


@require_daily_actor
@require_http_methods(["GET"])
def tomorrow_preview_json(request):
    """Poll target: returns tomorrow's checklist preview as JSON if ready,
    including a pre-rendered HTML fragment for in-place DOM swap.
    """
    participant = request.daily_participant
    today = _resolve_today(request)
    suggestion = _latest_suggestion_for_day(participant, today)
    if not suggestion:
        return JsonResponse({"ready": False, "reason": "no_suggestion_yet"})
    tomorrow_preview = {
        "questions": suggestion.proposed_questions or [],
        "note": suggestion.suggestion_text,
        "suggestion_id": suggestion.id,
    } if suggestion.proposed_questions else None
    html = render_to_string(
        "daily/_tomorrow_card.html",
        {
            "tomorrow_preview": tomorrow_preview,
            "debug_as_of": request.GET.get("as_of") if settings.DEBUG else None,
        },
        request=request,
    )
    return JsonResponse({
        "ready": True,
        "suggestion_id": suggestion.id,
        "note": suggestion.suggestion_text,
        "questions": suggestion.proposed_questions or [],
        "has_mutation": suggestion.proposed_questions is not None,
        "html": html,
    })


@require_daily_actor
@require_http_methods(["POST"])
def reject_tomorrow(request):
    """Discard the proposed mutation: tomorrow inherits today's checklist
    instead of the AI's proposal. Implemented by marking the suggestion
    DISMISSED, which the auto-apply path already excludes.
    """
    participant = request.daily_participant
    today = _resolve_today(request)
    ci = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    if not ci:
        return redirect(f"/daily/checkin/{_as_of_query(request)}")
    count = ci.suggestions.exclude(status=CoachSuggestion.STATUS_DISMISSED).update(
        status=CoachSuggestion.STATUS_DISMISSED,
        responded_at=timezone.now(),
    )
    if count:
        messages.success(request, "Tomorrow stays as it is today.")
    return redirect(f"/daily/checkin/{_as_of_query(request)}")


@require_daily_actor
@require_http_methods(["POST"])
def modify_tomorrow(request):
    """Free-text refinement: re-run the coach with the user's tweak."""
    participant = request.daily_participant
    today = _resolve_today(request)
    refinement = request.POST.get("refinement", "").strip()
    if not refinement:
        messages.error(request, "Tell the coach what you'd like to change.")
        return redirect(f"/daily/checkin/{_as_of_query(request)}")

    ci = DailyCheckIn.objects.filter(participant=participant, date=today).first()
    if not ci:
        messages.error(request, "Submit today's check-in first.")
        return redirect(f"/daily/checkin/{_as_of_query(request)}")

    # Mark current pending suggestion superseded so the new one becomes
    # the active proposal.
    ci.suggestions.exclude(status=CoachSuggestion.STATUS_DISMISSED).update(
        status=CoachSuggestion.STATUS_DISMISSED,
        responded_at=timezone.now(),
    )

    # Synchronous (mod_wsgi-safe — see checkin() for why).
    _run_coach(ci.id, refinement)
    messages.success(request, "Coach reworked tomorrow's plan.")
    return redirect(f"/daily/checkin/{_as_of_query(request)}")


@require_daily_actor
@require_http_methods(["POST"])
def respond_to_suggestion(request, suggestion_id):
    participant = request.daily_participant
    suggestion = get_object_or_404(
        CoachSuggestion,
        id=suggestion_id,
        check_in__participant=participant,
    )
    response = request.POST.get("response", "")
    if response == "dismissed":
        if suggestion.status == CoachSuggestion.STATUS_APPLIED and suggestion.applied_version:
            _revert_applied_suggestion(participant, suggestion)
        suggestion.status = CoachSuggestion.STATUS_DISMISSED
    else:
        return redirect(f"/daily/checkin/{_as_of_query(request)}")
    suggestion.responded_at = timezone.now()
    suggestion.save(update_fields=["status", "responded_at"])
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
    suggestion_text, proposed_questions, model_name, cost_usd = result

    CoachSuggestion.objects.create(
        check_in=check_in,
        suggestion_text=suggestion_text,
        proposed_questions=proposed_questions,
        rationale=f"refinement: {refinement}" if refinement else "",
        status=CoachSuggestion.STATUS_PENDING,
        model_name=model_name,
        cost_usd=cost_usd,
    )
