"""
Per-participant timezone resolution.

The app's day boundary ("today", the streak, the badge reset) must be each
user's LOCAL day, not the single server tz (US/Pacific). A participant stores
an IANA tz name (e.g. "America/New_York"), captured from their browser; until
captured it's blank and we fall back to the server tz.

`participant_today(participant)` is the one source of truth for "what day is it
for this person right now" — use it instead of `timezone.localdate()` anywhere
the answer should be the user's local day.

`estimated_wake_hour(participant)` / `target_morning_hour(participant)` learn
each participant's typical wake time from their own activity history, so the
overnight coach note + badge push can land just BEFORE they wake instead of at
a fixed server hour. See docstrings below for the estimation method.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

logger = logging.getLogger(__name__)

# --- wake-hour estimation --------------------------------------------------
# Tunable constants for estimated_wake_hour(). Kept here (not buried in the
# function) so they're easy to find and adjust without reading the algorithm.
WAKE_LOOKBACK_DAYS = 21     # how much history to scan for "first activity"
WAKE_MIN_DAYS = 5           # minimum days-with-data required to trust the estimate
WAKE_QUANTILE = 0.20        # low quantile of first-activity hour (robust to insomnia outliers)
WAKE_CLAMP_MIN_HOUR = 0.0   # sane local-hour window an estimate must fall inside
WAKE_CLAMP_MAX_HOUR = 11.0
FALLBACK_MORNING_HOUR = 6   # local hour used when there isn't enough signal to estimate


def participant_tz(participant):
    """The participant's tzinfo, or None to mean 'use the server default'.
    A blank/invalid stored tz falls back to None (server tz) rather than
    raising — a bad value must never break the page or the cron."""
    name = (getattr(participant, "timezone", "") or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("daily.tz: participant %s has invalid timezone %r",
                       getattr(participant, "id", "?"), name)
        return None


def participant_today(participant) -> date:
    """The current LOCAL date for this participant. Falls back to the server's
    localdate when the participant has no (valid) stored timezone."""
    tz = participant_tz(participant)
    if tz is None:
        return timezone.localdate()
    return timezone.now().astimezone(tz).date()


def participant_local_hour(participant) -> int:
    """The participant's current local hour (0-23). Used by the hourly badge
    job to fire 'morning' pushes at ~6am in each user's own timezone."""
    tz = participant_tz(participant)
    now = timezone.now()
    local = now.astimezone(tz) if tz is not None else timezone.localtime(now)
    return local.hour


def is_valid_iana(name: str) -> bool:
    """True if `name` is a resolvable IANA timezone (e.g. 'America/New_York')."""
    if not name or not isinstance(name, str) or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def _now_in(tz) -> datetime:  # pragma: no cover - tiny helper, kept for symmetry
    return timezone.now().astimezone(tz) if tz is not None else timezone.localtime()


def _low_quantile(values: list[float], q: float) -> float:
    """Linear-interpolation quantile (same convention as numpy's default
    'linear' method) — pure Python so this module has no extra dependency.
    `values` need not be sorted. Callers must ensure `values` is non-empty.
    """
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _first_activity_hours_by_local_day(participant, since) -> dict[date, float]:
    """For each local day with any activity since `since` (a UTC-aware
    datetime), the fractional local hour (0.0-23.999) of the EARLIEST
    activity that day, across every signal we track:
      - CoachChatMessage (role='user') — a message the person typed
      - DailyCheckIn.submitted_at — they opened and submitted the checklist
      - CoachSuggestion.responded_at — they acted on a coach suggestion

    Imported lazily to avoid a module-level import cycle (models -> ... -> tz).
    """
    from ..models import CoachChatMessage, CoachSuggestion, DailyCheckIn

    tz = participant_tz(participant)

    def local_hour(dt: datetime) -> tuple[date, float]:
        local = dt.astimezone(tz) if tz is not None else timezone.localtime(dt)
        return local.date(), local.hour + local.minute / 60 + local.second / 3600

    timestamps: list[datetime] = []
    timestamps.extend(
        CoachChatMessage.objects
        .filter(participant=participant, role=CoachChatMessage.ROLE_USER, created_at__gte=since)
        .values_list("created_at", flat=True)
    )
    timestamps.extend(
        DailyCheckIn.objects
        .filter(participant=participant, submitted_at__gte=since)
        .values_list("submitted_at", flat=True)
    )
    timestamps.extend(
        CoachSuggestion.objects
        .filter(check_in__participant=participant, responded_at__gte=since)
        .values_list("responded_at", flat=True)
    )

    first_by_day: dict[date, float] = {}
    for ts in timestamps:
        if ts is None:
            continue
        day, hour = local_hour(ts)
        if day not in first_by_day or hour < first_by_day[day]:
            first_by_day[day] = hour
    return first_by_day


def estimated_wake_hour(participant) -> float | None:
    """A robust estimate of `participant`'s typical wake hour (fractional,
    local time), learned from their own activity — or None if there isn't
    enough signal to trust an estimate.

    Method: look at the last WAKE_LOOKBACK_DAYS days. For each local day that
    has any activity, take the hour of the FIRST activity that day (earliest
    of: a user chat message, a submitted check-in, a responded-to coach
    suggestion). Across those per-day "first activity" hours, take the
    WAKE_QUANTILE (default 20th percentile) — a robust low quantile, so one
    insomniac 3am outlier doesn't drag the whole estimate down the way a min()
    or mean() would.

    Requires at least WAKE_MIN_DAYS distinct days of data; otherwise returns
    None (caller should fall back to FALLBACK_MORNING_HOUR). The result is
    clamped to [WAKE_CLAMP_MIN_HOUR, WAKE_CLAMP_MAX_HOUR]; an estimate outside
    that window (e.g. someone whose earliest touch is always mid-afternoon) is
    treated as untrustworthy for a "wake time" signal and also falls back.
    """
    since = timezone.now() - timedelta(days=WAKE_LOOKBACK_DAYS)
    first_by_day = _first_activity_hours_by_local_day(participant, since)

    if len(first_by_day) < WAKE_MIN_DAYS:
        return None

    estimate = _low_quantile(list(first_by_day.values()), WAKE_QUANTILE)

    if not (WAKE_CLAMP_MIN_HOUR <= estimate <= WAKE_CLAMP_MAX_HOUR):
        return None

    return estimate


def target_morning_hour(participant) -> int:
    """The local HOUR (0-23, integer) at which the overnight coach note and
    badge push should land for this participant: just before they typically
    wake, so the app is the first thing on their phone.

    A manually set `participant.morning_target_hour` (admin override, 0-23)
    wins outright — no estimation, no clamps. This is how a target in the
    20-23 range is expressed: "push the evening BEFORE, for the next local
    day" (e.g. 23 = an 11pm push for someone who wakes ~1am — a moment the
    learned path can't produce, since its estimate clamps to 0-11). The badge
    job (send_daily_badges --hourly) owns the cross-midnight semantics of
    evening targets.

    Otherwise: floor(estimated wake hour) - 1, clamped at 0 (never wraps to
    the previous day). Falls back to FALLBACK_MORNING_HOUR when there isn't
    enough activity history to estimate a wake time (see estimated_wake_hour).
    """
    override = getattr(participant, "morning_target_hour", None)
    if override is not None:
        return override
    wake = estimated_wake_hour(participant)
    if wake is None:
        return FALLBACK_MORNING_HOUR
    return max(0, int(wake) - 1)
