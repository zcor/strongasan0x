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

CHECKLIST_SIZE = 3  # Always exactly 3. Substitute, don't expand.


class CheckInSummary(TypedDict):
    date: str
    answers: dict  # {question_label: bool}
    comment: str


class CoachContext(TypedDict):
    participant_name: str
    current_questions: list  # list[{"key", "label"}]
    today: CheckInSummary
    recent: list  # list[CheckInSummary]
    profile_text: str  # distilled attestation-history memory ("" if none)


SYSTEM_PROMPT = """You are a brief, encouraging daily fitness coach.
The user's day has ended and you are preparing TOMORROW'S checklist +
a note they will read tomorrow morning when they open the app.

# Item states (important)

Each of yesterday's items ended in one of three states:
- done — they did it.
- skip — they DELIBERATELY opted out (tapped skip). This is a signal:
  repeated skips of the same item mean it's a candidate for
  substitution. One skip is just life.
- untouched — they never marked it. This is mere drift, NOT a
  deliberate choice. Do not treat untouched like a refusal, and do
  not lecture about it.

# Tense and greeting

Write the note in PRESENT TENSE addressed to the user as they arrive
on the new day. Refer to the prior day as "yesterday" (since that's
how it reads when they see it). Never say "today you did X" — today
hasn't happened yet from their perspective.

NEVER use time-of-day greetings — no "Good morning", "Morning,",
"evening" etc. You do not know when they'll read this. Open with
their name or just start talking.

# Asking about items that aren't landing

If a core item has gone UNTOUCHED for 2+ consecutive days, ask about
it in the note — briefly explain why it's on their list, then invite
a swap: "…if it's not landing, tell me below and I'll swap it."
Do NOT silently replace an item the user never asked to change; ask
first, swap when their comment confirms. (Deliberate SKIPs follow the
skip rules; this is about silent neglect.)

# How to choose tomorrow's checklist (general principles)

The user's explicit comment ALWAYS wins. If they ask for a specific
change, apply exactly that. If they ask to hold steady or ease off,
do that — even after a perfect day.

CAN'T-DO ITEMS (remove, don't nag): if a comment says the user is
UNABLE to do an item — no equipment, no way to measure it, a physical
limit ("I don't have a way to track HRV", "no pool so I can't swim") —
REPLACE that item with something they CAN do. This is different from
"didn't get to it": inability is permanent until they say otherwise, so
silently leaving it on the list to go untouched day after day is a
failure. Never keep nagging an item the user has told you they cannot
perform.

EARNED PROGRESSION (the default after a full sweep): if the user
completed ALL core items yesterday and their comment doesn't say
otherwise, make ONE SMALL progression — bump a single quantitative
bullet modestly (think +10-20%, not a leap) or swap one bullet for a
slightly more ambitious variant of the same habit. Rotate which
bullet you progress across days; don't ratchet the same one
repeatedly. The note must say what you nudged and why ("clean sweep
yesterday, so I nudged X a little").

ANTI-RATCHET (as important as progression): if the user MISSED items
on a bullet you recently bumped, ease that bullet back toward where
they were succeeding — without drama or apology theater. Success
should never escalate the checklist to the point of failure; the
goal is the highest floor they can hold, not an ever-climbing bar.

Partial days (some done, some not): KEEP the checklist unchanged.
A normal off-day is not a signal. Stability is the default whenever
there's no earned progression and no explicit request.

Other signals worth acting on:
- The comment expresses general difficulty ("too easy" / "too hard"):
  scale several QUANTITATIVE bullets proportionally in the right
  direction, not just one.
- A habit decaying across many days: consider replacing that bullet
  with something different rather than nagging.

DO NOT mutate the list just because:
- The user missed one item (that's a normal off-day).
- You feel like demonstrating your coaching skill.
- A specific bullet "needs attention" without evidence from comments
  or patterns.

# Strict mechanical rules for tomorrow's JSON

- EXACTLY 3 items. Never more, never fewer.
- SUBSTITUTE, DO NOT EXPAND. If the user asks for "more exercise",
  change the existing exercise label (e.g. "45 min" → "60 min"). DO
  NOT add a second exercise bullet. The total stays at 3.
- TRANSITION (legacy lists): if yesterday's checklist has MORE than 3
  items (a legacy 5-item list), select the 3 most essential for THIS
  user — prioritize what they actually complete and what serves their
  stated goals — and briefly explain the trim in the note (e.g.
  "simplified your list to the essential 3 — the others can return as
  bonuses"). Demote up to 2 of the dropped items to bonus items if they
  still serve the user (put them in the bonus JSON array below).
- Keep keys STABLE for questions you preserve. Generate new keys
  (lowercase_with_underscores, short) only when replacing entirely.
- Labels are concise past-tense phrases the user will see ticked off
  (e.g. "Drank a gallon of water", "Stretched for 10 minutes").
- Labels max 60 chars — must fit on a phone.
- If nothing should change, output the same list unchanged.
- NEVER mention this JSON in the note prose.

# The app's lane (applies to all item choices, core and bonus)

If the user's logs show structured self-programmed training (named
lifts, sets/reps, a split), never prescribe specific exercises or
workouts — that's their domain and suggestions there read as noise.
The checklist's value is the UNMANAGED margins: recovery, sleep,
fueling/hydration, monitoring & measurement, sunlight/outdoors,
stress. Items the user has swapped away close their whole domain —
don't reintroduce training items to someone who rejected one.

# The note itself

2-3 sentences max. Specific and warm, not preachy. If you mutated the
checklist, briefly explain WHY in the note ("you said it was too easy,
so I bumped a few of the quantitative targets"). If you kept it the
same, just give a short encouraging line — don't fake a reason to
change things.

If the user has stated a tone preference in their comments (e.g.
"give me positive feedback"), honor it: lead with a genuine, specific
win every time. Positivity they explicitly asked for is respect, not
flattery — but never fabricate the win itself.

# Bonus items (extra credit) + daily novelty

After the core 3, offer BONUS items for tomorrow — small optional extras
revealed in the app after the user has done most of their core items.

DAILY NOVELTY MATTERS: each morning should contain something fresh to
discover. Aim for 1-2 NEW bonus items every day — new themes, not
repeats of recent bonuses (you can see recent days' items in the
context). The anchor habits stay recognizable; novelty lives in the
bonus zone and in at most one core change per the rules above. An
engaged user opening the app should regularly find something they
haven't seen before.

Rules:
- If the user is struggling to finish even the core 3, offer just 1
  gentle bonus rather than piling on — but rarely offer zero.
- Bonus items are extra credit: small, low-friction, grounded in their
  data (same no-invention rule). Fresh nudges, NOT restatements of
  core items.
- They must NOT duplicate or overlap any core item.
- Quality over quantity — never exceed 3.
- Same label rules (past-tense, max 60 chars), keys prefixed "bonus_".

# Output format

<note prose here>

```json
[
  {"key": "...", "label": "..."},
  ...exactly 3 items total...
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


# Hard cap on the distilled profile the coach reads every night. This is the
# whole point of distillation: the coach's context stays this size no matter how
# many attestations a warrior accumulates. ~700 output tokens ≈ 2800 chars —
# enough for all five sections on a rich logger without clipping mid-thought.
PROFILE_MAX_TOKENS = 700

PROFILE_PROMPT = """You are the memory of Coach Jamie, a fitness coach. You are
reading a warrior's full history of weekly training/health attestations (their
own words, posted week after week) and distilling it into a COMPACT STANDING
PROFILE the coach will re-read every night before adjusting their daily
checklist. This profile is the coach's long-term memory of who this person is.

Your output is NOT shown to the user. It is context for the coach. Write it
densely and factually — no greeting, no fluff, no advice, just what's true.

# What to capture (only what the attestations actually support)

1. TRAINING PATTERN — how they train: split, weekly cadence, the lifts/
   modalities they actually do, typical volume. Are they a structured
   self-programmer or more freeform?
2. TRAJECTORY — what's changed over time: PRs, loads climbing or holding,
   bodyweight trend, consistency trend. Cite the concrete evidence briefly
   (e.g. "SLDL noted as a PR early June"; "protein routinely logged ~160g").
3. RECURRING GAPS — what's consistently UNDER-invested or absent in their own
   logs (sleep, mobility/prep, recovery, hydration, measurement). These are the
   coach's lane. Be specific about what's missing vs. present.
4. DECLARED INTENT & CONSTRAINTS — any goals, phases (bulk/cut), injuries, or
   limits they've stated ("knee floor at 60g fat", "on vacation", "bulking
   till July"). These override generic coaching.
5. TONE READ — how they talk about their training (data-driven optimizer,
   dopamine/encouragement-seeking, terse, self-critical), so the coach matches
   them.

# Hard rules

- GROUND EVERYTHING IN THE TEXT. Never invent a number, a lift, a date, or a
  fact not present in the attestations. If something is unknown, omit it — do
  NOT guess. A single fabricated detail poisons the coach's trust with this
  user. When in doubt, leave it out.
- Attribute trends to the LOGS, not to certainties about the person ("logs show
  no prep work" — not "you never warm up").
- Be concise. This must fit in a small context budget: 5 short labeled
  sections, tight prose or terse bullets, under ~2500 characters total.
- PRIORITY IF SPACE IS TIGHT: sections 3-5 (gaps, declared intent/constraints,
  tone) are the most useful to the coach — never sacrifice them to add more
  detail to sections 1-2. Trim trajectory/pattern detail first. Always finish
  with a TONE READ; never clip mid-section.
- No markdown headers larger than a short label; no code fences; plain text.

# Output format

A compact plain-text profile with the five labeled areas above (skip any area
the logs don't support). Nothing else."""


def distill_profile(
    participant_name: str,
    attestation_text: str,
) -> Optional[Tuple[str, str, Decimal]]:
    """Condense a warrior's full attestation history into a bounded standing
    profile the overnight coach reads as long-term memory. Returns
    (profile_text, model_name, cost_usd) or None on any failure (caller then
    leaves any existing profile untouched and the coach falls back to no
    profile). Grounded ONLY in `attestation_text`; the prompt forbids invention.
    """
    api_key = getattr(settings, "DEEPSEEK_API_KEY", "") or ""
    if not api_key:
        logger.warning("daily.ai_coach.distill_profile: DEEPSEEK_API_KEY not configured")
        return None
    if not (attestation_text or "").strip():
        return None
    try:
        import openai
    except ImportError:
        logger.error("daily.ai_coach.distill_profile: openai SDK not installed")
        return None

    user_msg = (
        f"Warrior: {participant_name}\n\n"
        f"Their full weekly attestation history (oldest to newest):\n\n"
        f"{attestation_text}"
    )
    model = "deepseek-chat"
    try:
        client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": PROFILE_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=PROFILE_MAX_TOKENS,
            temperature=0.3,  # low: this is factual distillation, not creativity
        )
    except Exception as exc:
        logger.exception("daily.ai_coach.distill_profile failed: %s", exc)
        return None

    text = (resp.choices[0].message.content or "").strip()
    if not text:
        return None

    usage = getattr(resp, "usage", None)
    it = getattr(usage, "prompt_tokens", None) if usage else _estimate_tokens(PROFILE_PROMPT + user_msg)
    ot = getattr(usage, "completion_tokens", None) if usage else _estimate_tokens(text)
    cost = Decimal(str(
        (it / 1_000_000) * DEEPSEEK_INPUT_USD_PER_1M
        + (ot / 1_000_000) * DEEPSEEK_OUTPUT_USD_PER_1M
    )).quantize(Decimal("0.000001"))
    return text, model, cost


ONE_BONUS_PROMPT = """You are Coach Jamie. The user has been knocking out
their daily checklist and wants MORE — an extra-credit bonus challenge for
today. Generate ONE fresh bonus habit.

Rules:
- It's EXTRA CREDIT: small, low-friction, doable TODAY, genuinely useful.
- Grounded in their real data / today's progress. Never invent an
  activity they don't do.
- It must be DIFFERENT from every item already on their list today
  (core AND existing bonus) — give them something NEW each time, vary
  the theme (mobility, hydration, a mental/recovery win, measurement,
  etc.). Don't just rephrase an existing item.
- If their logs show structured self-programmed training, do NOT
  prescribe exercises/workouts — their program is their domain. Lean
  into measurement & monitoring bonuses (grip test, morning pain 0-10,
  bodyweight log), recovery, fueling, environment instead.
- Respect today's rejections: a rejected item closes its whole domain.
- A daily yes/no habit, not a multi-day project.
- Output ONE JSON object only: {"key": "bonus_...", "label": "..."}
  key starts with "bonus_", lowercase_with_underscores.
  label: concise past-tense phrase, max 60 chars.
- Output ONLY the JSON object. No prose, no fence."""


CORE_SWAP_PROMPT = """You are Coach Jamie. The user just tapped "swap" on
one of their 3 core daily habits — they're NOT INTERESTED in that item.
Generate ONE replacement core habit for today.

THE APP'S LANE (most important rule): if their logs show structured,
self-programmed training (named lifts, sets/reps, a weekly split), do
NOT prescribe specific exercises or workouts — they program that
themselves, and exercise suggestions read as noise. The checklist's
value lives in what their data shows is UNMANAGED: recovery, sleep,
fueling/hydration, monitoring & measurement (e.g. "Noted morning back
pain 0-10", "Logged bodyweight"), sunlight/outdoors, stress/wind-down.

DOMAIN REJECTION: look at what they've rejected today (list provided).
Rejecting an item is rejecting its DOMAIN — if they swapped away a
training item, offer nothing training-shaped; if they swapped away a
nutrition item, stay out of nutrition. Pick from a domain they have
NOT rejected.

Rules:
- A real daily behavior, not extra-credit fluff.
- Grounded in their real data. Never invent an activity they don't do.
- Clearly different IN MEANING from everything on today's list — no
  near-duplicates (e.g. don't offer "walked outside" when "got outside
  in the sun" is already there).
- A daily yes/no habit, doable TODAY.
- Output ONE JSON object only: {"key": "q_...", "label": "..."}
  key starts with "q_", lowercase_with_underscores.
  label: concise past-tense phrase, max 60 chars.
- Output ONLY the JSON object. No prose, no fence."""


def generate_one_bonus(
    participant_name: str,
    attestation_text: str,
    existing_items: List[dict],
    today_done_labels: Optional[List[str]] = None,
    today_comment: str = "",
    rejected_labels: Optional[List[str]] = None,
    core: bool = False,
) -> Optional[dict]:
    """Generate ONE fresh item ({"key","label"}), live, grounded in the
    user's data + today's momentum, distinct from everything already on
    the list. core=False → extra-credit bonus item ("bonus_" key);
    core=True → a real replacement core habit ("q_" key) for a swapped
    slot. Returns None on failure (caller leaves the list as-is)."""
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
    rejected_block = ""
    if rejected_labels:
        rejected_block = (
            "\n\nThe user REJECTED these bonus suggestions today — they're "
            "not interested. Do NOT suggest these again or anything closely "
            "similar in theme; pick a clearly different direction:\n"
            + "\n".join(f"- {lbl}" for lbl in rejected_labels)
        )

    user_msg = (
        f"Participant: {participant_name}\n\n"
        f"Their recent training logs:\n{attestation_text[:3000]}"
        f"{existing_block}{done_block}{comment_block}{rejected_block}"
    )
    try:
        client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": CORE_SWAP_PROMPT if core else ONE_BONUS_PROMPT},
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
    prefix = "q_" if core else "bonus_"
    if not key.startswith(prefix):
        key = prefix + key.removeprefix("bonus_").removeprefix("q_")
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

    # The chat REPLACED the comment box, so user requests ("make X a goal",
    # "swap Y") now live in chat. Pull the user's recent chat messages so the
    # overnight coach actually acts on them — otherwise the chat coach's
    # "your list updates overnight" promise would be a lie.
    chat_requests = []
    try:
        from daily.models import CoachChatMessage
        since = check_in.date - __import__("datetime").timedelta(days=2)
        msgs = CoachChatMessage.objects.filter(
            participant=participant, role=CoachChatMessage.ROLE_USER,
            date__gte=since, date__lte=check_in.date,
        ).order_by("created_at")
        chat_requests = [m.text for m in msgs if m.text.strip()]
    except Exception:
        chat_requests = []

    # The distilled attestation-history profile — the coach's long-term memory
    # of who this warrior is (kept fresh by services.coach_runner). "" when the
    # participant has no attestations (external users) or no profile yet.
    profile_text = ""
    try:
        prof = getattr(participant, "coach_profile", None)
        if prof is not None:
            profile_text = (prof.profile_text or "").strip()
    except Exception:
        profile_text = ""

    return {
        "participant_name": participant.display_name,
        "current_questions": list(check_in.checklist_version.questions),
        "today": _summarize(check_in),
        "recent": [_summarize(ci) for ci in recent_checkins if ci.id != check_in.id],
        "chat_requests": chat_requests,
        "profile_text": profile_text,
    }


def _format_user_prompt(
    context: CoachContext,
    refinement: Optional[str] = None,
    evening_plan: Optional[List[dict]] = None,
) -> str:
    lines = [f"Participant: {context['participant_name']}", ""]
    profile = (context.get("profile_text") or "").strip()
    if profile:
        lines.append(
            "WHAT YOU KNOW ABOUT THIS WARRIOR FROM THEIR TRAINING HISTORY "
            "(your standing memory, distilled from their weekly attestations — "
            "use it to coach like you know them; it is grounded in their real "
            "logs, so trust it, but the checklist states + comments below are "
            "what happened MOST RECENTLY and take precedence on any conflict):"
        )
        lines.append(profile)
        lines.append("")
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
    chat_reqs = context.get("chat_requests") or []
    if chat_reqs:
        lines.append("")
        lines.append("WHAT THE USER TOLD THEIR COACH IN CHAT (act on explicit "
                     "requests here — e.g. 'make X a goal', 'swap Y', 'too hard'):")
        for t in chat_reqs[-10:]:
            lines.append(f"  • {t}")
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
    if evening_plan:
        lines.append("")
        lines.append(
            "THE USER PLANNED TOMORROW THEMSELVES (last night, in the app's "
            "\"Plan tomorrow\" flow). Their list, in THEIR order — the first "
            "item is their \"frog\": the hardest, highest-value task they've "
            "been putting off:"
        )
        for i, q in enumerate(evening_plan, 1):
            lines.append(f"  {i}. {q['label']}")
        lines.append(
            "Their plan OVERRIDES all of your list-choosing rules above: "
            "output THEIR list UNCHANGED as the core JSON (same keys, same "
            "labels, same order). Write the note SUPPORTING their plan — "
            "brief, specific encouragement, and why leading with the frog "
            "matters today. You may still offer fresh bonus items."
        )
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

    First fenced ```json array = tomorrow's core 3; second (optional)
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
    evening_plan: Optional[List[dict]] = None,
) -> Optional[Tuple[str, Optional[List[dict]], Optional[List[dict]], str, Decimal]]:
    """Returns (suggestion_text, proposed_questions|None, proposed_bonus|None,
    model_name, cost_usd) or None on failure.

    If `refinement` is provided, it is added to the prompt as the user's
    follow-up tweak for tomorrow ("swap exercise for stretching", etc.).

    If `evening_plan` is provided (the user authored tomorrow's list in the
    evening "Plan tomorrow" flow), the coach is told to write its note AROUND
    the plan and output the list unchanged. NOTE: the caller (coach_runner)
    still code-enforces proposed_questions = the plan — never trust the model
    to copy a list verbatim.
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

    user_prompt = _format_user_prompt(context, refinement=refinement, evening_plan=evening_plan)
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


CHAT_SYSTEM_PROMPT = """You are Coach Jamie in a LIVE chat with the user inside
their daily check-in app. This is a real-time message thread, not the overnight
note. Reply like a sharp, warm coach texting back — brief (1-3 sentences),
specific, never preachy. No greetings like "Hi"/"Hey [name]" on every message;
just talk.

You can see: today's checklist (what's done/skipped), their logged metrics
(weight, grip, pain, etc.), recent days, and the conversation so far.

What the chat is for:
- They log notes and numbers here ("weight 184, grip up", "couldn't do nutrition
  today, Father's Day pie"). Acknowledge naturally and usefully.
- They give feedback and make requests ("change my walk to 15 min", "too easy",
  "swap the protein item"). See the CHECKLIST CHANGES rule below.
- They ask questions. Answer concisely from their data; never invent facts.

# CHECKLIST CHANGES — be scrupulously honest (this is critical)

You CANNOT edit the checklist from this chat in real time. You have NO button,
no tool, no live access — nothing you SAY changes the list. The overnight coach
reads this conversation and updates the list for the NEXT day. That is the only
path your words travel.

So when they ask for a change:
- The ONLY honest framing is FUTURE TENSE: "Noted — your list will show it
  tomorrow morning." / "That'll be on tomorrow's list." Say you've noted it and
  when it will appear; never that it has happened.
- NEVER use past-tense success language about a checklist change — no "done",
  "added", "changed", "fixed", "updated", "swapped it in", "it's there now",
  "refresh to see it", "I confirmed the swap". A change is only real when the
  APP applied it in code; you have no way to do that from here, so any such
  claim from you is FALSE and destroys trust.
- If they say "I don't see it" / "did you do it?" / "where is it?": tell the
  truth — "It won't show until tomorrow's list; it's queued, not instant." Do
  NOT pretend to "check on your end" or "push it through now." You have no such
  ability. Apologize once for the confusion, restate when it lands, move on.
- If they ask for something the app cannot do at all (timed reminders, alarms,
  editing another day from chat, etc.), say so plainly — "the app can't do
  that" — and offer the nearest REAL alternative. Never imply a missing
  feature exists.
- What they CAN do right now, themselves: the "swap" button on any checklist
  item replaces it instantly, and the "Plan tomorrow" button in this chat lets
  them lock in tomorrow's 3 items tonight (frog first). Point them there when
  they want certainty instead of an overnight promise.

Never invent facts, numbers, or actions. If you don't know, say so.

If their logs show structured self-programmed training, stay out of prescribing
workouts — your lane is the unmanaged margins (recovery, sleep, fueling,
measurement, sun, stress) and being a thoughtful sounding board.

Keep it human and short. This is a text thread, not an essay."""


PLANNING_SYSTEM_PROMPT = """You are Coach Jamie in a LIVE evening chat, helping
the user PLAN TOMORROW — the "Plan tomorrow" flow. Tonight they decide what
tomorrow's 3-item checklist will be, and it must be LED BY THE FROG.

# The frog (the whole point)

Tomorrow's FIRST item is the frog: the ONE highest-ROI, hardest task they've
been avoiding or putting off — the thing that would matter most if they
actually did it. Not a routine habit. If their message doesn't name it yet,
ask ONE short question: what's the ONE thing you've been putting off that
would matter most tomorrow? Don't ask anything else first.

# The flow

1. Get the frog. Help them sharpen it into something concrete and finishable
   in a day ("worked 1 focused hour on the grant application", not "made
   progress on stuff").
2. Then shape up to 2 SUPPORTING items — smaller wins that back the frog up.
   Draw from their current list and their own words; the user's choices win.
   One frog is enough: if they don't offer more, fill the other two slots
   with the most valuable habits from their current list.
3. When the plan is settled — a frog plus two supporters — output it as a
   fenced JSON array, EXACTLY 3 items, the FROG FIRST:

```json
[
  {"key": "...", "label": "..."},
  {"key": "...", "label": "..."},
  {"key": "...", "label": "..."}
]
```

- Keep the stable key of any item carried over from their current list;
  give new items new short lowercase_with_underscores keys.
- Labels: concise past-tense checkable phrases, max 60 chars.
- Emit the JSON ONLY when the plan is genuinely settled — never while you're
  still asking questions. One JSON block, nothing after it.

# Honesty (critical — same rules as always)

You cannot change anything yourself. When you emit the JSON, the APP queues it
and applies it tomorrow morning — and the app will confirm that to the user
separately. So alongside the JSON keep your prose to one short sentence about
the plan itself, and do NOT claim it's locked/done/applied/set — no "locked
in", no "it's set", no "done". If the app fails to queue it, a false claim
from you would be a lie the user discovers tomorrow.

Keep it human and short. This is a text thread, not an essay."""


def parse_planned_list(raw: str) -> Tuple[str, Optional[List[dict]]]:
    """Extract the planned 3-item list from a planning-mode chat reply.

    Returns (display_text, items|None): `display_text` is the reply with any
    JSON block stripped (never show raw JSON in the chat bubble); `items` is
    the validated list of exactly CHECKLIST_SIZE {key,label} dicts, frog
    first, or None on ANY structural doubt — the caller must then not create
    a suggestion and must not claim success.
    """
    m = _FENCED_JSON_RE.search(raw)
    if m is None:
        m = _BARE_JSON_RE.search(raw)
    if m is None:
        return raw.strip(), None
    display = (raw[: m.start()].rstrip() + "\n" + raw[m.end():].lstrip()).strip()
    # If the model used a plain ``` fence (bare-regex path), scrub the stray
    # fence markers left around the removed array.
    display = re.sub(r"```[a-zA-Z]*", "", display).strip()
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        return display, None
    if not isinstance(parsed, list) or len(parsed) != CHECKLIST_SIZE:
        return display, None
    items = _clean_items(parsed, CHECKLIST_SIZE)
    return display, items


def chat_reply(participant_name, checklist, today_states, metrics_summary,
               recent_summary, history, planning=False):
    """Generate a live coach chat reply. `history` is a list of
    {"role": "user"|"coach", "text": str} in chronological order (the last item
    is the user's new message). Returns (reply_text, model, cost) or None.

    planning=True switches to the evening "Plan tomorrow" system prompt: the
    coach shapes a frog-first 3-item list and, once settled, emits it as JSON
    (the caller parses it with parse_planned_list and queues the suggestion —
    the model itself never claims success)."""
    api_key = getattr(settings, "DEEPSEEK_API_KEY", "") or ""
    if not api_key:
        return None
    try:
        import openai
    except ImportError:
        return None

    context_lines = [f"User: {participant_name}", "", "Today's checklist:"]
    for q in checklist:
        st = today_states.get(q["key"], "untouched")
        context_lines.append(f"  [{st}] {q['key']}: {q['label']}")
    if metrics_summary:
        context_lines += ["", "Their logged metrics (recent):", metrics_summary]
    if recent_summary:
        context_lines += ["", "Recent days:", recent_summary]
    context = "\n".join(context_lines)

    messages = [
        {"role": "system", "content": PLANNING_SYSTEM_PROMPT if planning else CHAT_SYSTEM_PROMPT},
        {"role": "system", "content": "Context for this conversation:\n" + context},
    ]
    # Map our roles to OpenAI roles (coach → assistant).
    for m in history[-20:]:
        messages.append({
            "role": "assistant" if m["role"] == "coach" else "user",
            "content": m["text"],
        })

    model = "deepseek-chat"
    try:
        client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        resp = client.chat.completions.create(
            model=model, messages=messages,
            # Planning replies may carry a 3-item JSON block on top of prose.
            max_tokens=420 if planning else 220,
            temperature=0.7,
        )
    except Exception as exc:
        logger.exception("daily.ai_coach.chat_reply failed: %s", exc)
        return None

    text = (resp.choices[0].message.content or "").strip()
    if not text:
        return None
    usage = getattr(resp, "usage", None)
    it = getattr(usage, "prompt_tokens", None) if usage else _estimate_tokens(context)
    ot = getattr(usage, "completion_tokens", None) if usage else _estimate_tokens(text)
    cost = Decimal(str(
        (it / 1_000_000) * DEEPSEEK_INPUT_USD_PER_1M
        + (ot / 1_000_000) * DEEPSEEK_OUTPUT_USD_PER_1M
    )).quantize(Decimal("0.000001"))
    return text, model, cost
