"""Helpers for determining the active weekly roll call."""
from datetime import timedelta
from django.utils import timezone
from rollcall.models import WeeklyRollCall
from rollcall.utils.timezone_utils import get_week_end_for_attestation


def get_active_roll_call(reference_dt=None, allow_create=True):
    """
    Return the roll call corresponding to the given datetime (default: now).

    Uses Pacific timezone for all calculations. During the attestation window
    (Friday 5 PM - Monday 6 PM Pacific), returns the roll call for the week
    that just ended.

    Late attestation policy: if the computed target week hasn't started yet
    (target Sunday is in the future) and the prior week's roll call exists
    but isn't published, assign to the prior week instead. This accepts
    late entries through ~Wednesday without manual reassignment.

    Args:
        reference_dt: The datetime to use for lookup (default: now)
        allow_create: If True, create the roll call if it doesn't exist

    Returns:
        WeeklyRollCall or None
    """
    if reference_dt is None:
        reference_dt = timezone.now()

    # Get the Sunday (week_end_date) this attestation should be assigned to
    target_sunday = get_week_end_for_attestation(reference_dt)

    # Late attestation policy: if target is a future week and the prior week
    # is still unpublished, attribute to the prior week as a late entry.
    today = reference_dt.date() if hasattr(reference_dt, 'date') else reference_dt
    if target_sunday > today:
        prior_sunday = target_sunday - timedelta(days=7)
        prior_roll_call = WeeklyRollCall.objects.filter(
            week_end_date=prior_sunday, is_published=False
        ).first()
        if prior_roll_call:
            return prior_roll_call

    # Look up the roll call by week_end_date
    roll_call = WeeklyRollCall.objects.filter(week_end_date=target_sunday).first()

    if roll_call or not allow_create:
        return roll_call

    # Create new roll call
    week_start = target_sunday - timedelta(days=6)  # Monday of that week

    roll_call, _ = WeeklyRollCall.objects.get_or_create(
        week_start_date=week_start,
        defaults={
            "week_end_date": target_sunday,
            "substack_url": f"https://strongasan0x.substack.com/p/week-of-{week_start.strftime('%Y-%m-%d')}",
            "full_text": f"Roll Call for the week of {week_start.strftime('%Y-%m-%d')}",
            "is_published": False,
        },
    )

    return roll_call
