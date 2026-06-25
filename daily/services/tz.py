"""
Per-participant timezone resolution.

The app's day boundary ("today", the streak, the badge reset) must be each
user's LOCAL day, not the single server tz (US/Pacific). A participant stores
an IANA tz name (e.g. "America/New_York"), captured from their browser; until
captured it's blank and we fall back to the server tz.

`participant_today(participant)` is the one source of truth for "what day is it
for this person right now" — use it instead of `timezone.localdate()` anywhere
the answer should be the user's local day.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

logger = logging.getLogger(__name__)


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
