"""
AI coach for the daily checklist.

Returns (suggestion_text, proposed_questions_or_None, model_name, cost_usd).
The proposed_questions field is a list of {"key": str, "label": str}
dicts the AI suggests for tomorrow — if present, the daily view
auto-applies it as a new ChecklistVersion the next day unless the user
dismissed the suggestion first.

Plain-dict in, plain-tuple out — no Django ORM types leak across.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date as date_cls
from decimal import Decimal
from typing import List, Optional, Tuple, TypedDict

from django.conf import settings

logger = logging.getLogger(__name__)

DEEPSEEK_INPUT_USD_PER_1M = 0.14
DEEPSEEK_OUTPUT_USD_PER_1M = 0.28

CHECKLIST_SIZE = 5  # Always exactly 5. Substitute, don't expand.


class CheckInSummary(TypedDict):
    date: str
    answers: dict  # {question_label: bool}
    comment: str


class CoachContext(TypedDict):
    participant_name: str
    current_questions: list  # list[{"key", "label"}]
    today: CheckInSummary
    recent: list  # list[CheckInSummary]


SYSTEM_PROMPT = """You are a brief, encouraging daily fitness coach.
The user just submitted today's checklist (5 yes/no items) and you are
preparing TOMORROW'S checklist + a note they will read tomorrow morning
when they sit down to check in.

# Tense

Write the note in PRESENT TENSE addressed to the user as they arrive
on the new day. Refer to the prior day as "yesterday" (since that's
how it reads when they see it). Never say "today you did X" — today
hasn't happened yet from their perspective.

# How to choose tomorrow's checklist (general principles)

Default: KEEP the same 5 questions, unchanged. Most days require no
mutation. A stable checklist is the goal; mutation is the exception.

Mutate the list ONLY when there is a clear signal in the user's
comment or in the recent-days pattern. Examples of clear signals
(not an exhaustive list):

- The comment names a specific change ("longer exercise tomorrow",
  "swap nutrition for protein 180g", "add a stretching item"). Apply
  exactly that change, no more.
- The comment expresses a general difficulty judgment ("too easy",
  "this is becoming a slog", "I'm crushing it"). Scale several
  QUANTITATIVE bullets proportionally, not just one. If "too easy",
  bump 2 or 3 of the bullets that have numbers in them. If "too
  hard", scale them DOWN by a similar amount across multiple bullets.
- The recent-days pattern shows the user has nailed an item for a
  week+: it's earned a small stretch on THAT item specifically.
- The recent-days pattern shows a habit decaying: consider replacing
  the failing bullet with something different rather than nagging.

DO NOT mutate the list just because:
- The user missed one item (that's a normal off-day).
- You feel like demonstrating your coaching skill.
- A specific bullet (e.g. wins, water) "needs attention" without
  evidence from comments or patterns.

# Strict mechanical rules for tomorrow's JSON

- EXACTLY 5 items. Never more, never fewer.
- SUBSTITUTE, DO NOT EXPAND. If the user asks for "more exercise",
  change the existing exercise label (e.g. "45 min" → "60 min"). DO
  NOT add a second exercise bullet. The total stays at 5.
- Keep keys STABLE for questions you preserve. Generate new keys
  (lowercase_with_underscores, short) only when replacing entirely.
- Labels are concise past-tense phrases the user will see ticked off
  (e.g. "Drank a gallon of water", "Stretched for 10 minutes").
- Labels max 60 chars — must fit on a phone.
- If nothing should change, output the same list unchanged.
- NEVER mention this JSON in the note prose.

# The note itself

2-3 sentences max. Specific and warm, not preachy. If you mutated the
checklist, briefly explain WHY in the note ("you said it was too easy,
so I bumped a few of the quantitative targets"). If you kept it the
same, just give a short encouraging line — don't fake a reason to
change things.

# Output format

<note prose here>

```json
[
  {"key": "...", "label": "..."},
  ...exactly 5 items total...
]
```

Never invent facts not in the data."""


STRETCH_PROMPT = """You are Coach Jamie. You're choosing ONE personalized
"stretch" habit to add to someone's daily checklist — the single
highest-leverage thing that would most IMPROVE their health, based on
what their own data shows is MISSING or under-invested.

You are given two sources:
1. Their recent training/attestation logs.
2. Any free-text comments they've typed into this checklist app before.

Find the genuine gap. Examples of good reasoning (do NOT just copy these):
- Someone with huge training volume but no logged sleep/mobility →
  recovery is the gap. Suggest a mobility or sleep item.
- Someone with tons of cardio/steps but light strength stimulus →
  suggest a protein or strength item.
- Someone who repeatedly comments about an injury → suggest a
  prehab/rehab item for that area.

Rules:
- Output ONE habit as a single JSON object: {"key": "...", "label": "..."}
- key: short lowercase_with_underscores.
- label: a concise past-tense daily checkable phrase, max 60 chars
  (e.g. "Did 10 minutes of mobility", "Hit 150g protein", "Slept 7+ hours").
- It must be a DAILY yes/no habit, not a one-time task.
- Ground it in their actual data. Do not invent activities they never do.
- Output ONLY the JSON object. No prose, no code fence, no explanation."""


def derive_stretch_item(
    participant_name: str,
    attestation_text: str,
    prior_comments: Optional[List[str]] = None,
    existing_items: Optional[List[dict]] = None,
) -> Optional[dict]:
    """Pick ONE personalized stretch habit ({"key","label"}) grounded in the
    user's attestation logs + their prior in-app comments, that does NOT
    overlap with `existing_items` already on the checklist. Returns None on
    any failure (caller should fall back to a sensible default item).
    """
    api_key = getattr(settings, "DEEPSEEK_API_KEY", "") or ""
    if not api_key:
        return None
    try:
        import openai
    except ImportError:
        return None

    comments_block = ""
    if prior_comments:
        joined = "\n".join(f"- {c}" for c in prior_comments if c)
        if joined:
            comments_block = f"\n\nTheir prior comments in this app:\n{joined}"

    existing_block = ""
    if existing_items:
        joined = "\n".join(f"- {q.get('label', '')}" for q in existing_items)
        if joined:
            existing_block = (
                f"\n\nThe checklist ALREADY contains these items — your "
                f"stretch item MUST cover a DIFFERENT area (do not duplicate "
                f"sleep if sleep is here, recovery if recovery is here, etc.):\n{joined}"
            )

    user_msg = (
        f"Participant: {participant_name}\n\n"
        f"Their recent training logs:\n{attestation_text[:4000]}"
        f"{comments_block}"
        f"{existing_block}"
    )
    try:
        client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": STRETCH_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=120,
            temperature=0.4,
        )
    except Exception as exc:
        logger.exception("daily.ai_coach.derive_stretch_item failed: %s", exc)
        return None

    raw = (resp.choices[0].message.content or "").strip()
    # Strip a code fence if the model added one anyway.
    m = re.search(r"\{[\s\S]*?\}", raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    key = str(obj.get("key", "")).strip()
    label = str(obj.get("label", "")).strip()
    if not key or not label or len(label) > 60 or len(key) > 40:
        return None
    return {"key": key, "label": label}


def build_coach_context(participant, check_in, recent_checkins) -> CoachContext:
    def _label_map(version):
        return {q["key"]: q["label"] for q in version.questions}

    def _summarize(ci) -> CheckInSummary:
        labels = _label_map(ci.checklist_version)
        return {
            "date": ci.date.isoformat() if isinstance(ci.date, date_cls) else str(ci.date),
            "answers": {
                labels.get(a.question_key, a.question_key): bool(a.value)
                for a in ci.answers.all()
            },
            "comment": ci.comment or "",
        }

    return {
        "participant_name": participant.display_name,
        "current_questions": list(check_in.checklist_version.questions),
        "today": _summarize(check_in),
        "recent": [_summarize(ci) for ci in recent_checkins if ci.id != check_in.id],
    }


def _format_user_prompt(context: CoachContext, refinement: Optional[str] = None) -> str:
    lines = [f"Participant: {context['participant_name']}", ""]
    lines.append("Today's checklist (the one they answered):")
    for q in context["current_questions"]:
        lines.append(f"  - {q['key']}: {q['label']}")
    today = context["today"]
    lines.append("")
    lines.append(f"Today's answers ({today['date']}):")
    for label, value in today["answers"].items():
        lines.append(f"  {'✓' if value else '✗'} {label}")
    if today["comment"]:
        lines.append(f"Comment: {today['comment']}")
    if context["recent"]:
        lines.append("")
        lines.append(f"Last {len(context['recent'])} days (most recent first):")
        for r in context["recent"]:
            score = sum(1 for v in r["answers"].values() if v)
            total = len(r["answers"])
            lines.append(f"  {r['date']}: {score}/{total}" + (f' — "{r["comment"]}"' if r["comment"] else ""))
    if refinement:
        lines.append("")
        lines.append("THE USER HAS REFINED THEIR REQUEST FOR TOMORROW:")
        lines.append(f"  {refinement}")
        lines.append("Apply this refinement to tomorrow's checklist.")
    lines.append("")
    lines.append("Now respond with the note + tomorrow's JSON checklist.")
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_FENCED_JSON_RE = re.compile(r"```json\s*(\[[\s\S]*?\])\s*```", re.MULTILINE)
_BARE_JSON_RE = re.compile(r"(\[\s*\{[\s\S]*?\}\s*\])")


def _parse_response(raw: str) -> Tuple[str, Optional[List[dict]]]:
    """Split raw response into (prose, parsed_questions_or_None).

    Strategy: find a JSON list in the response (preferring fenced
    ```json block), validate it loosely, return the prose part with the
    JSON stripped.
    """
    match = _FENCED_JSON_RE.search(raw)
    if match:
        json_text = match.group(1)
        prose = raw[: match.start()].rstrip()
    else:
        match = _BARE_JSON_RE.search(raw)
        if match:
            json_text = match.group(1)
            prose = (raw[: match.start()] + raw[match.end():]).strip()
        else:
            return raw.strip(), None

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        logger.warning("daily.ai_coach: JSON parse failed: %s", exc)
        return raw.strip(), None

    if not isinstance(parsed, list):
        return prose, None
    if len(parsed) != CHECKLIST_SIZE:
        logger.info(
            "daily.ai_coach: question count %d != %d, rejecting mutation",
            len(parsed), CHECKLIST_SIZE,
        )
        return prose, None

    cleaned: List[dict] = []
    seen_keys = set()
    for item in parsed:
        if not isinstance(item, dict):
            return prose, None
        key = str(item.get("key", "")).strip()
        label = str(item.get("label", "")).strip()
        if not key or not label or key in seen_keys:
            return prose, None
        if len(label) > 60 or len(key) > 40:
            return prose, None
        seen_keys.add(key)
        cleaned.append({"key": key, "label": label})

    return prose, cleaned


def generate_suggestion(
    context: CoachContext,
    refinement: Optional[str] = None,
) -> Optional[Tuple[str, Optional[List[dict]], str, Decimal]]:
    """Returns (suggestion_text, proposed_questions_or_None, model_name, cost_usd) or None.

    If `refinement` is provided, it is added to the prompt as the user's
    follow-up tweak for tomorrow ("swap exercise for stretching", etc.).
    """
    api_key = getattr(settings, "DEEPSEEK_API_KEY", "") or ""
    if not api_key:
        logger.warning("daily.ai_coach: DEEPSEEK_API_KEY not configured; skipping suggestion")
        return None

    try:
        import openai
    except ImportError:
        logger.error("daily.ai_coach: openai SDK not installed")
        return None

    user_prompt = _format_user_prompt(context, refinement=refinement)
    model = "deepseek-chat"

    try:
        client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=700,
            temperature=0.6,
        )
    except Exception as exc:
        logger.exception("daily.ai_coach: DeepSeek call failed: %s", exc)
        return None

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        return None

    suggestion_text, proposed_questions = _parse_response(raw)
    if not suggestion_text:
        suggestion_text = "Great work today. Keep going."

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", None) if usage else _estimate_tokens(SYSTEM_PROMPT + user_prompt)
    output_tokens = getattr(usage, "completion_tokens", None) if usage else _estimate_tokens(raw)
    cost = Decimal(str(
        (input_tokens / 1_000_000) * DEEPSEEK_INPUT_USD_PER_1M
        + (output_tokens / 1_000_000) * DEEPSEEK_OUTPUT_USD_PER_1M
    )).quantize(Decimal("0.000001"))

    return suggestion_text, proposed_questions, model, cost
