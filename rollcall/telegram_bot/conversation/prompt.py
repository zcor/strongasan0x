"""Prompt construction for the Sonnet classifier.

The cached system block is large and stable (persona + contest overview + warrior
list + decision rubric + few-shots + tool schema). The per-call user message is
small (current message + features). Cache strategy: 1-hour TTL on the system
block — prompt-caching skill says 5min default doesn't fit sporadic mid-week
chat. Pay 2x cache write cost once an hour; reads are 0.1x.

The classifier returns its result via tool_use (`record_classification`), forcing
structured JSON output with no regex parsing.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from rollcall.telegram_bot.conversation.persona import BOT_PERSONA

logger = logging.getLogger(__name__)


# Tool schema — forces structured output. The classifier MUST call this tool.
RECORD_CLASSIFICATION_TOOL = {
    "name": "record_classification",
    "description": "Record the classification verdict for this message.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_attestation": {
                "type": "boolean",
                "description": "True if this message is a fitness attestation (a warrior reporting their weekly training/health labors).",
            },
            "attestation_confidence": {
                "type": "number",
                "description": "Confidence in the is_attestation verdict, 0.0 to 1.0.",
            },
            "should_reply": {
                "type": "boolean",
                "description": "True if Bull should reply to this message. Always true for direct mentions, replies to Bull, and DMs (when chat_type is private). False for routine attestations and most group chatter unless the warrior clearly addressed Bull.",
            },
            "reply_reason": {
                "type": "string",
                "description": "Brief explanation of why should_reply is true or false.",
            },
            "intent": {
                "type": "string",
                "enum": [
                    "attestation",
                    "question_self",
                    "question_other_warrior",
                    "question_general",
                    "banter",
                    "admin",
                    "other",
                ],
                "description": "The primary intent: attestation (logging labors), question_self (asking about own data), question_other_warrior (asking about another warrior), question_general (asking about contest/leaderboard/etc), banter (casual chat), admin (bot management), other.",
            },
            "target_warrior": {
                "type": ["string", "null"],
                "description": "If the message asks about a specific warrior other than the sender, the canonical warrior name. Null otherwise.",
            },
            "mentions_private_data": {
                "type": "boolean",
                "description": "True if the message references the sender's private uploads (labs, screenshots) DM'd previously.",
            },
        },
        "required": [
            "is_attestation",
            "attestation_confidence",
            "should_reply",
            "reply_reason",
            "intent",
            "target_warrior",
            "mentions_private_data",
        ],
    },
}


# --- Static system prompt (cached) ---

CONTEST_OVERVIEW = """About the Strong-as-an-0x contest:

A weekly fitness contest. Warriors post "attestations" — narrative reports of their week's training — to Telegram and Discord. The attestation window opens Friday 5pm Pacific and closes Monday 6pm Pacific. Outside that window, attestations are rare but can be late submissions for the prior week.

A typical attestation is structured (day-by-day breakdown, lifts, cardio, body comp, sleep) but format varies wildly. Some are bullet lists, some are bare day names with weight-reps notation like "Tuesday\\n225-3, 245-1", some are prose paragraphs, some are screenshots from Garmin/Strava/Hevy. Length ranges from 50 to 2000 characters.

Things that LOOK like attestations but are not:
- Discussion *about* attestations ("did you submit yours?")
- Coaching banter ("nice numbers!")
- Meta about the contest (rules, deadlines)
- Bot commands or bot reply text
- Single-line workout brags without weekly context (a one-line PR brag mid-week is usually banter, not an attestation)

Things that ARE attestations even though they look unusual:
- Bare day-of-week labels followed by lift/cardio data
- Screenshots with extracted text
- Stream-of-consciousness summaries with no structure but real metrics
- Late posts mid-week explicitly tagged as "for last week"
"""


REPLY_RUBRIC = """About when Bull should reply:

Bull should reply (should_reply = true) when:
- chat_type is "private" (DM) — always reply unless the message is empty or pure noise
- The message directly @-mentions @StrongAsAn0x or @StrongAsAn0xBot
- The message is a reply to one of Bull's prior messages
- A warrior asks an obvious question that fits Bull's domain (their progress, the leaderboard, contest data) and the question is clearly directed at Bull (e.g., "Bull, how's my bench?" in a group)

Bull should NOT reply (should_reply = false) when:
- The message is an attestation being posted (warrior is logging, not asking)
- The message is group chatter, banter, or warrior-to-warrior conversation that doesn't involve Bull
- The message is a question directed at another warrior, not Bull
- The message is a bot command (/status, /view, etc) — those are handled by command handlers, not Bull

Default conservative: in a busy group, if it's ambiguous whether the warrior is talking to Bull or to another warrior, set should_reply = false. Bull does not interject.

A message can be BOTH an attestation AND should_reply = true (rare: e.g., a warrior posts their attestation with an explicit question to Bull tacked on the end). Both fields are independent.
"""


HOMEWORK_EXAMPLES = """Examples (do not echo these in your reasoning, just use them as calibration):

Example 1 — Classic attestation, weekend:
Message: "Sunday: 5 mile run, 8:30 pace. Monday: bench 225x5x3, OHP 135x5x3..."
Verdict: is_attestation=true (high conf), should_reply=false, intent=attestation

Example 2 — Bare day-name format (Spencer-style):
Message: "Tuesday\\n225-3, 245-1, 265-1\\nWednesday\\n315-5, 335-3"
Verdict: is_attestation=true (high conf), should_reply=false, intent=attestation

Example 3 — Coaching banter that is NOT an attestation:
Message: "Nice numbers Spencer! That bench progression is sick."
Verdict: is_attestation=false, should_reply=false, intent=banter

Example 4 — Question to Bull about own data, in DM:
Message: "How has my bench progressed over the last 6 months?"
Verdict: is_attestation=false, should_reply=true, intent=question_self

Example 5 — Question to Bull about another warrior, in group with mention:
Message: "@StrongAsAn0x how is Jones doing this season?"
Verdict: is_attestation=false, should_reply=true, intent=question_other_warrior, target_warrior="Jones | Rarestone Compass"

Example 6 — Group chatter, no Bull involvement:
Message: "Anyone else dealing with shoulder pain on heavy bench?"
Verdict: is_attestation=false, should_reply=false, intent=banter

Example 7 — Late attestation, mid-week:
Message: "Posting last week late: Sat: long run 12mi. Sun: rest. Mon-Fri: 5 lifts..."
Verdict: is_attestation=true (high conf), should_reply=false, intent=attestation

Example 8 — Meta question about the contest, in group, no mention:
Message: "When does this week's roll close again?"
Verdict: is_attestation=false, should_reply=false (no Bull mention), intent=question_general

Example 9 — Same meta question, in DM:
Message: "When does this week's roll close again?"
Verdict: is_attestation=false, should_reply=true, intent=question_general

Example 10 — Direct coaching ask in group with mention:
Message: "@StrongAsAn0x I missed the gym today due to a work call — what should I do at home instead?"
Verdict: is_attestation=false, should_reply=true, intent=question_self
(Bull should answer with a concrete suggestion, not deflect. The warrior is asking for help, not chatter.)
"""


def build_system_prompt(warrior_list_block: str) -> str:
    """Assemble the full static system prompt. Cached as a single block."""
    return "\n\n".join(
        [
            BOT_PERSONA,
            CONTEST_OVERVIEW,
            "Known warriors (canonical names with aliases):\n" + warrior_list_block,
            REPLY_RUBRIC,
            HOMEWORK_EXAMPLES,
            "Always call the record_classification tool with your verdict. Do not write any text response.",
        ]
    )


# --- Warrior list cache (refreshed lazily) ---

_warrior_list_cache: dict = {"text": "", "fetched_at": None}
_CACHE_TTL = timedelta(hours=1)


def get_warrior_list_block() -> str:
    """Return a formatted warrior-list string, refreshed at most hourly.

    Fetches `linked_name` from TelegramUserMapping and DiscordUserMapping,
    plus canonical names from the most recent RollCallRanking.
    """
    now = datetime.now()
    cached_at = _warrior_list_cache["fetched_at"]
    if cached_at and (now - cached_at) < _CACHE_TTL and _warrior_list_cache["text"]:
        return _warrior_list_cache["text"]

    try:
        from rollcall.models import (
            DiscordUserMapping,
            RollCallRanking,
            TelegramUserMapping,
            WeeklyRollCall,
        )
        from django.db import close_old_connections

        close_old_connections()

        # Canonical names from the most recent published roll call
        latest = WeeklyRollCall.objects.filter(is_published=True).order_by("-week_end_date").first()
        canonical = set()
        if latest:
            canonical = set(
                RollCallRanking.objects.filter(weekly_roll_call=latest).values_list("name", flat=True)
            )

        tg_aliases: dict = {}
        for m in TelegramUserMapping.objects.filter(is_active=True).exclude(linked_name=""):
            tg_aliases.setdefault(m.linked_name, set()).add(
                f"@{m.telegram_username}" if m.telegram_username else m.telegram_first_name
            )
        for m in DiscordUserMapping.objects.filter(is_active=True).exclude(linked_name=""):
            tg_aliases.setdefault(m.linked_name, set()).add(
                m.discord_display_name or m.discord_username
            )

        # Union canonical with whatever has aliases (some warriors may not be in latest roll)
        all_names = sorted(canonical | set(tg_aliases.keys()))

        lines = []
        for name in all_names:
            aliases = tg_aliases.get(name, set())
            if aliases:
                lines.append(f"- {name} (aliases: {', '.join(sorted(aliases))})")
            else:
                lines.append(f"- {name}")

        text = "\n".join(lines) if lines else "(no warriors linked yet)"
        _warrior_list_cache["text"] = text
        _warrior_list_cache["fetched_at"] = now
        return text
    except Exception:
        logger.exception("Warrior list refresh failed; returning stale or empty")
        return _warrior_list_cache["text"] or "(warrior list unavailable)"


# --- Per-call user message (NOT cached) ---


def build_user_message(
    *,
    text: str,
    chat_type: str,
    is_mention_or_reply: bool,
    pacific_now_iso: str,
    is_weekend_window: bool,
    sender_display: str,
    sender_linked_warrior: Optional[str],
    recent_history: list,
    has_image: bool,
    image_caption: Optional[str] = None,
) -> str:
    """Build the per-call user message (small, not cached)."""
    history_lines = []
    for turn in recent_history[-8:]:
        # turn shape: {"role": "warrior"|"bot", "name": str, "text": str}
        role_label = turn.get("role", "warrior")
        name = turn.get("name", "?")
        snippet = (turn.get("text") or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        history_lines.append(f"  [{role_label}] {name}: {snippet}")
    history_block = "\n".join(history_lines) if history_lines else "  (none)"

    target_warrior_label = sender_linked_warrior or "(not linked to any warrior)"

    parts = [
        "Classify this Telegram message.",
        f"Now (Pacific): {pacific_now_iso}",
        f"Inside attestation window (Fri 5pm-Mon 6pm Pacific): {is_weekend_window}",
        f"Chat type: {chat_type}",
        f"Direct mention or reply to Bull: {is_mention_or_reply}",
        f"Sender: {sender_display}  Warrior: {target_warrior_label}",
        f"Has image attachment: {has_image}",
    ]
    if image_caption:
        parts.append(f"Image caption: {image_caption}")
    parts.append("")
    parts.append("Recent chat history (oldest to newest):")
    parts.append(history_block)
    parts.append("")
    parts.append("Message:")
    parts.append("---")
    parts.append(text)
    parts.append("---")
    parts.append("")
    parts.append("Call record_classification with your verdict.")
    return "\n".join(parts)
