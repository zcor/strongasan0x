"""Claude CLI quota circuit breaker.

Detects "usage limit", "credit balance", "rate_limit", "overloaded",
"payment required" patterns in stderr/stdout and writes a cooldown marker
to logs/claude_cli_state.json. State persists across launchd restarts.

Pattern from fleet-commodore/commodore.py:1563-1606.
"""
import json
import logging
import time
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)


def _state_path() -> Path:
    return Path(settings.BASE_DIR) / "logs" / "claude_cli_state.json"


def claude_is_available() -> bool:
    """True if no active cooldown."""
    p = _state_path()
    if not p.exists():
        return True
    try:
        data = json.loads(p.read_text())
        until = float(data.get("unavailable_until", 0))
        return time.time() >= until
    except Exception:
        logger.exception("Couldn't read claude_cli_state.json")
        return True


def mark_claude_unavailable(hours: float = None):
    """Open the circuit breaker for `hours` (default from settings, 6h)."""
    if hours is None:
        hours = getattr(settings, "CONVERSATION_CLAUDE_CIRCUIT_HOURS", 6)
    until = time.time() + hours * 3600
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"unavailable_until": until, "set_at": time.time()}))
    logger.warning("Claude CLI circuit OPEN until %s (%.1fh)", until, hours)


def looks_like_quota_error(text: str) -> bool:
    if not text:
        return False
    s = text.lower()
    return any(k in s for k in (
        "usage limit",
        "monthly usage",
        "rate_limit",
        "rate limit",
        "overloaded",
        "payment required",
        "credit balance is too low",
        "claude ai usage limit reached",
        "quota",
    ))
