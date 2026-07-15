"""Persisted streak rollups.

Answer rows remain authoritative. We maintain a compact daily DONE count and a
participant-level streak cache when answers change, keeping historical work off
the dashboard GET while preserving the existing adaptive streak behavior.
"""
from datetime import date, timedelta

from django.db.models import Count, Q

from ..models import DailyCheckIn, DailyCheckInAnswer, DailyParticipant
from .tz import participant_today


STREAK_FLOOR = 1
STREAK_CAP = 3
STREAK_HISTORY_DAYS = 400
STREAK_CACHE_VERSION = 1


def _streak_bar(done_counts: list[int]) -> int:
    """Median of the latest engaged days, clamped to the existing 1–3 bar."""
    if not done_counts:
        return STREAK_FLOOR
    ordered = sorted(done_counts)
    median = ordered[len(ordered) // 2]
    return max(STREAK_FLOOR, min(STREAK_CAP, median))


def calculate_streak_state(
    daily_counts: dict[date, int],
    today: date,
) -> dict:
    """Calculate the cache fields from compact date → DONE-count rollups."""
    recent_engaged = [
        count
        for day, count in sorted(daily_counts.items(), reverse=True)
        if day <= today and count > 0
    ][:21]
    bar = _streak_bar(recent_engaged)

    anchor = today if daily_counts.get(today, 0) >= bar else today - timedelta(days=1)
    streak = 0
    day = anchor
    while daily_counts.get(day, 0) >= bar:
        streak += 1
        day -= timedelta(days=1)

    return {
        "streak_count": streak,
        "streak_through_date": anchor if streak else None,
        "streak_bar": bar,
        "streak_cache_version": STREAK_CACHE_VERSION,
    }


def update_checkin_done_count(
    check_in: DailyCheckIn,
    done_count: int | None = None,
) -> int:
    """Refresh one day's compact rollup and return its current value."""
    if done_count is None:
        done_count = check_in.answers.filter(
            state=DailyCheckInAnswer.STATE_DONE
        ).count()
    done_count = max(0, int(done_count))
    if check_in.done_count != done_count:
        DailyCheckIn.objects.filter(pk=check_in.pk).update(done_count=done_count)
        check_in.done_count = done_count
    return done_count


def refresh_streak_cache(
    participant: DailyParticipant,
    *,
    today: date | None = None,
    changed_check_in: DailyCheckIn | None = None,
    done_count: int | None = None,
) -> int:
    """Rebuild one participant's cache after an answer mutation.

    The recalculation happens on writes, not page views, and reads only compact
    `(date, done_count)` values rather than checklist JSON and answer objects.
    """
    today = today or participant_today(participant)
    if changed_check_in is not None:
        update_checkin_done_count(changed_check_in, done_count)

    counts = dict(
        DailyCheckIn.objects.filter(
            participant=participant,
            date__gte=today - timedelta(days=STREAK_HISTORY_DAYS),
            date__lte=today,
        ).values_list("date", "done_count")
    )
    state = calculate_streak_state(counts, today)
    changed = {
        field: value
        for field, value in state.items()
        if getattr(participant, field) != value
    }
    if changed:
        DailyParticipant.objects.filter(pk=participant.pk).update(**changed)
        for field, value in changed.items():
            setattr(participant, field, value)
    return state["streak_count"]


def current_streak(participant: DailyParticipant, today: date) -> int:
    """Read the cache, rebuilding only after a future algorithm-version bump."""
    if participant.streak_cache_version != STREAK_CACHE_VERSION:
        return refresh_streak_cache(participant, today=today)
    if participant.streak_through_date in (today, today - timedelta(days=1)):
        return participant.streak_count
    return 0


def rebuild_daily_rollups(participant: DailyParticipant) -> int:
    """Repair every daily DONE count for one participant; return rows changed."""
    check_ins = list(
        DailyCheckIn.objects.filter(participant=participant).annotate(
            calculated_done_count=Count(
                "answers",
                filter=Q(answers__state=DailyCheckInAnswer.STATE_DONE),
            )
        )
    )
    changed = []
    for check_in in check_ins:
        if check_in.done_count != check_in.calculated_done_count:
            check_in.done_count = check_in.calculated_done_count
            changed.append(check_in)
    if changed:
        DailyCheckIn.objects.bulk_update(changed, ["done_count"])
    return len(changed)
