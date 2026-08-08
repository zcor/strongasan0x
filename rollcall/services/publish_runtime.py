"""Shared configuration helpers for Roll Call syndication."""

from django.conf import settings


DEFAULT_TELEGRAM_CHAT_ID = -1003122619283


def telegram_roll_call_chat_id() -> int:
    """Return the configured Roll Call supergroup, with a legacy fallback."""
    configured = str(getattr(settings, "TELEGRAM_ATTESTATION_CHANNEL_ID", "") or "").strip()
    if not configured:
        return DEFAULT_TELEGRAM_CHAT_ID
    try:
        return int(configured)
    except ValueError:
        return DEFAULT_TELEGRAM_CHAT_ID
