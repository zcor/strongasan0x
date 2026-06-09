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
The user's day has ended and you are preparing TOMORROW'S checklist +
a note they will read tomorrow morning when they open the app.

# Item states (important)

Each of yesterday's 5 items ended in one of three states:
- done — they did it.
- skip — they DELIBERATELY opted out (tapped skip). This is a signal:
  repeated skips of the same item mean it's a candidate for
  substitution. One skip is just life.
- untouched — they never marked it. This is mere drift, NOT a
  deliberate choice. Do not treat untouched like a refusal, and do
  not lecture about it.

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

# Bonus items (optional, extra credit)

After the core 5, offer BONUS items for tomorrow — small optional extras
revealed in the app only after the user has done several core items. They
keep an engaged user discovering new challenges without bloating the
core 5. Rules:
- If the user completed most/all of their core items recently (a sign
  they have capacity for more), offer 1-2 bonus items. If they're
  struggling to finish the core 5, offer 0 — don't pile on.
- Bonus items are extra credit: small, low-friction, grounded in their
  data (same no-invention rule). Each should be a genuine, fresh nudge
  toward improvement — NOT a restatement of a core item.
- They must NOT duplicate or overlap any core item.
- Vary them over time as the user engages; this is the "keeps feeding me
  new things" mechanic. But quality over quantity — 1 great bonus beats
  3 filler ones. Never exceed 3.
- Same label rules (past-tense, max 60 chars), keys prefixed "bonus_".

# Output format

<note prose here>

```json
[
  {"key": "...", "label": "..."},
  ...exactly 5 items total...
]
```

```json
[
  {"key": "bonus_...", "label": "..."},
  ...0 to 3 bonus items; emit an empty array [] if none...
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


ONE_BONUS_PROMPT = """You are Coach Jamie. The user has been knocking out
their daily checklist and wants MORE — an extra-credit bonus challenge for
today. Generate ONE fresh bonus habit.

Rules:
- It's EXTRA CREDIT: small, low-friction, doable TODAY, genuinely useful.
- Grounded in their real data / today's progress. Never invent an
  activity they don't do.
- It must be DIFFERENT from every item already on their list today
  (core AND existing bonus) — give them something NEW each time, vary
  the theme (mobility, hydration, a mental/recovery win, a small skill
  rep, etc.). Don't just rephrase an existing item.
- A daily yes/no habit, not a multi-day project.
- Output ONE JSON object only: {"key": "bonus_...", "label": "..."}
  key starts with "bonus_", lowercase_with_underscores.
  label: concise past-tense phrase, max 60 chars.
- Output ONLY the JSON object. No prose, no fence."""


def generate_one_bonus(
    participant_name: str,
    attestation_text: str,
    existing_items: List[dict],
    today_done_labels: Optional[List[str]] = None,
    today_comment: str = "",
) -> Optional[dict]:
    """Generate ONE fresh bonus item ({"key","label"}), live, grounded in
    the user's data + today's momentum, distinct from everything already
    on the list. Returns None on failure (caller leaves the pile as-is)."""
    api_key = getattr(settings, "DEEPSEEK_API_KEY", "") or ""
    if not api_key:
        logger.warning("daily.ai_coach.generate_one_bonus: DEEPSEEK_API_KEY not configured")
        return None
    try:
        import openai
    except ImportError:
        logger.error("daily.ai_coach.generate_one_bonus: openai SDK not installed")
        return None

    existing_block = ""
    if existing_items:
        joined = "\n".join(f"- {q.get('label', '')}" for q in existing_items)
        existing_block = f"\n\nAlready on today's list (do NOT duplicate any of these):\n{joined}"
    done_block = ""
    if today_done_labels:
        done_block = f"\n\nDone so far today: {', '.join(today_done_labels)}"
    comment_block = f"\n\nToday's comment: {today_comment}" if today_comment else ""

    user_msg = (
        f"Participant: {participant_name}\n\n"
        f"Their recent training logs:\n{attestation_text[:3000]}"
        f"{existing_block}{done_block}{comment_block}"
    )
    try:
        client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": ONE_BONUS_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=100,
            temperature=0.7,  # higher temp for variety across refills
        )
    except Exception as exc:
        logger.exception("daily.ai_coach.generate_one_bonus failed: %s", exc)
        return None

    raw = (resp.choices[0].message.content or "").strip()
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
    if not key.startswith("bonus_"):
        key = "bonus_" + key
    # Ensure uniqueness vs existing keys (append a short suffix if needed).
    existing_keys = {q.get("key") for q in existing_items}
    if key in existing_keys:
        import hashlib
        key = key[:34] + "_" + hashlib.md5(label.encode()).hexdigest()[:5]
    return {"key": key, "label": label}


def build_coach_context(participant, check_in, recent_checkins) -> CoachContext:
    def _label_map(version):
        labels = {q["key"]: q["label"] for q in version.questions}
        for q in (version.bonus_questions or []):
            labels[q["key"]] = q["label"] + " (bonus)"
        return labels

    def _summarize(ci) -> CheckInSummary:
        labels = _label_map(ci.checklist_version)
        states = {a.question_key: a.state for a in ci.answers.all()}
        return {
            "date": ci.date.isoformat() if isinstance(ci.date, date_cls) else str(ci.date),
            # {label: "done"|"skip"|"untouched"} — every question gets an
            # entry even with no answer row (untouched = drift, not refusal).
            "answers": {
                labels.get(key, key): (
                    states[key] if states.get(key) in ("done", "skip") else "untouched"
                )
                for key in labels
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
    state_marks = {"done": "✓ done", "skip": "✕ SKIPPED (deliberate)", "untouched": "· untouched"}
    lines.append("")
    lines.append(f"Yesterday's final states ({today['date']}):")
    for label, state in today["answers"].items():
        lines.append(f"  {state_marks.get(state, state)} — {label}")
    if today["comment"]:
        lines.append(f"Comment: {today['comment']}")
    if context["recent"]:
        lines.append("")
        lines.append(f"Last {len(context['recent'])} days (most recent first):")
        for r in context["recent"]:
            done = sum(1 for v in r["answers"].values() if v == "done")
            skipped = [lbl for lbl, v in r["answers"].items() if v == "skip"]
            total = len(r["answers"])
            extra = f" — skipped: {', '.join(skipped)}" if skipped else ""
            extra += f' — "{r["comment"]}"' if r["comment"] else ""
            lines.append(f"  {r['date']}: {done}/{total} done{extra}")
    if refinement:
        lines.append("")
        lines.append("THE USER HAS REFINED THEIR REQUEST FOR TOMORROW:")
        lines.append(f"  {refinement}")
        lines.append("Apply this refinement to tomorrow's checklist.")
    lines.append("")
    lines.append("Now respond with the note + tomorrow's core JSON checklist + bonus JSON array.")
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_FENCED_JSON_RE = re.compile(r"```json\s*(\[[\s\S]*?\])\s*```", re.MULTILINE)
_BARE_JSON_RE = re.compile(r"(\[\s*\{[\s\S]*?\}\s*\])")


def _clean_items(parsed, max_items, seen_keys=None) -> Optional[List[dict]]:
    """Validate a parsed JSON list of {key,label} items. Returns None on
    any structural problem."""
    if not isinstance(parsed, list) or len(parsed) > max_items:
        return None
    cleaned: List[dict] = []
    seen = set(seen_keys or ())
    for item in parsed:
        if not isinstance(item, dict):
            return None
        key = str(item.get("key", "")).strip()
        label = str(item.get("label", "")).strip()
        if not key or not label or key in seen:
            return None
        if len(label) > 60 or len(key) > 40:
            return None
        seen.add(key)
        cleaned.append({"key": key, "label": label})
    return cleaned


def _parse_response(raw: str) -> Tuple[str, Optional[List[dict]], Optional[List[dict]]]:
    """Split raw response into (prose, core_questions|None, bonus|None).

    First fenced ```json array = tomorrow's core 5; second (optional)
    = bonus items (0-3). Bonus is best-effort: any doubt → None.
    """
    matches = list(_FENCED_JSON_RE.finditer(raw))
    if not matches:
        match = _BARE_JSON_RE.search(raw)
        if not match:
            return raw.strip(), None, None
        matches = [match]

    prose = raw[: matches[0].start()].rstrip()

    try:
        core_parsed = json.loads(matches[0].group(1))
    except json.JSONDecodeError as exc:
        logger.warning("daily.ai_coach: core JSON parse failed: %s", exc)
        return raw.strip(), None, None

    if not isinstance(core_parsed, list) or len(core_parsed) != CHECKLIST_SIZE:
        logger.info(
            "daily.ai_coach: core count %s != %d, rejecting mutation",
            len(core_parsed) if isinstance(core_parsed, list) else "?", CHECKLIST_SIZE,
        )
        return prose, None, None
    core = _clean_items(core_parsed, CHECKLIST_SIZE)
    if core is None:
        return prose, None, None

    bonus = None
    if len(matches) > 1:
        try:
            bonus_parsed = json.loads(matches[1].group(1))
            bonus = _clean_items(bonus_parsed, 3, seen_keys=[q["key"] for q in core])
            if bonus == []:
                bonus = None  # empty array → no bonus
        except json.JSONDecodeError:
            bonus = None

    return prose, core, bonus


def generate_suggestion(
    context: CoachContext,
    refinement: Optional[str] = None,
) -> Optional[Tuple[str, Optional[List[dict]], Optional[List[dict]], str, Decimal]]:
    """Returns (suggestion_text, proposed_questions|None, proposed_bonus|None,
    model_name, cost_usd) or None on failure.

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

    suggestion_text, proposed_questions, proposed_bonus = _parse_response(raw)
    if not suggestion_text:
        suggestion_text = "Great work yesterday. Keep going."

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", None) if usage else _estimate_tokens(SYSTEM_PROMPT + user_prompt)
    output_tokens = getattr(usage, "completion_tokens", None) if usage else _estimate_tokens(raw)
    cost = Decimal(str(
        (input_tokens / 1_000_000) * DEEPSEEK_INPUT_USD_PER_1M
        + (output_tokens / 1_000_000) * DEEPSEEK_OUTPUT_USD_PER_1M
    )).quantize(Decimal("0.000001"))

    return suggestion_text, proposed_questions, proposed_bonus, model, cost
