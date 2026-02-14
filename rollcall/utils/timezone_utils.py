"""
Centralized timezone utilities for consistent Pacific time handling.
All attestation window logic uses US/Pacific timezone.
"""
from datetime import timedelta
from zoneinfo import ZoneInfo
from django.conf import settings

PACIFIC_TZ = ZoneInfo("US/Pacific")
UTC_TZ = ZoneInfo("UTC")

# Attestation window boundaries (in hours, 24-hour format)
ATTESTATION_WINDOW_START_HOUR = getattr(settings, 'ATTESTATION_WEEKEND_START_HOUR', 17)  # Friday 5 PM
ATTESTATION_WINDOW_END_HOUR = 18  # Monday 6 PM
ATTESTATION_WINDOW_START_WEEKDAY = 4  # Friday (Monday=0)
ATTESTATION_WINDOW_END_WEEKDAY = 0  # Monday


def to_pacific(dt):
    """
    Convert any datetime to Pacific timezone.

    Args:
        dt: A datetime object (naive assumed UTC, or timezone-aware)

    Returns:
        datetime: Timezone-aware datetime in Pacific time
    """
    if dt is None:
        return None

    if dt.tzinfo is None:
        # Naive datetime - assume UTC
        dt = dt.replace(tzinfo=UTC_TZ)

    return dt.astimezone(PACIFIC_TZ)


def is_in_attestation_window(dt):
    """
    Check if a datetime falls within the attestation window.
    Window: Friday 5 PM Pacific through Monday 6 PM Pacific.

    Args:
        dt: A datetime object (will be converted to Pacific)

    Returns:
        bool: True if within attestation window
    """
    if dt is None:
        return False

    pacific_dt = to_pacific(dt)
    weekday = pacific_dt.weekday()  # Monday=0, Sunday=6
    hour = pacific_dt.hour

    # Friday after 5 PM (17:00)
    if weekday == ATTESTATION_WINDOW_START_WEEKDAY and hour >= ATTESTATION_WINDOW_START_HOUR:
        return True

    # Saturday (all day)
    if weekday == 5:
        return True

    # Sunday (all day)
    if weekday == 6:
        return True

    # Monday before 6 PM (18:00)
    if weekday == ATTESTATION_WINDOW_END_WEEKDAY and hour < ATTESTATION_WINDOW_END_HOUR:
        return True

    return False


def get_week_end_for_attestation(dt):
    """
    Determine which week (by week_end_date/Sunday) an attestation should be assigned to.

    During attestation window (Fri 5 PM - Mon 6 PM Pacific), attestations
    are assigned to the week that just ended (the Sunday within/before this window).

    Outside the window, attestations are assigned to the current week's Sunday.

    Args:
        dt: A datetime object (will be converted to Pacific)

    Returns:
        date: The Sunday (week_end_date) this attestation belongs to
    """
    if dt is None:
        from django.utils import timezone
        dt = timezone.now()

    pacific_dt = to_pacific(dt)
    current_date = pacific_dt.date()
    weekday = pacific_dt.weekday()  # Monday=0, Sunday=6

    if is_in_attestation_window(dt):
        # We're in the attestation window - assign to the week that just ended
        # Find the Sunday of this attestation window
        if weekday == 6:  # Sunday
            return current_date
        elif weekday == 0:  # Monday (before 6 PM, checked by is_in_attestation_window)
            return current_date - timedelta(days=1)  # Yesterday was Sunday
        elif weekday == 5:  # Saturday
            return current_date + timedelta(days=1)  # Tomorrow is Sunday
        else:  # Friday after 5 PM
            return current_date + timedelta(days=2)  # Sunday is in 2 days
    else:
        # Outside attestation window - assign to current week's Sunday
        # This handles Mon 6PM through Fri 5PM
        days_until_sunday = 6 - weekday
        return current_date + timedelta(days=days_until_sunday)
