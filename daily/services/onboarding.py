"""
In-app onboarding for NAKED users (no attestation history).

A user who arrives without context (a Telegram stranger/lurker, or anyone via a
future non-Telegram door) gets a tiny, no-typing-required questionnaire that
seeds their first checklist. The governing constraint is the app's simplicity
principle: at MOST three multiple-choice questions, every one skippable, with an
optional write-in. We never block the app behind it.

The flow:
  Q1  persona split  — the Spencer (strength) ↔ Amy (holistic) axis. The one
                       question that matters most; it picks the branch.
  Q2  refiner        — branches on Q1; names the user's focus area.
  Q3  cadence        — the daily floor (light / medium / heavy) → calibrates
                       how ambitious the seeded items are.

Answers map to a hand-written top-3 from SEED_LIBRARY (branch × focus × cadence,
with sensible fallbacks). Any write-in text is returned separately so the caller
can fold it into the user's first coach context — the overnight coach then
refines from the user's own words on day one.

Pure data + functions: no Django models imported here. The view owns persistence
(building the ChecklistVersion, stamping onboarded_at).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# --- Branch / focus / cadence vocabularies ---------------------------------

BRANCH_STRENGTH = "strength"
BRANCH_HOLISTIC = "holistic"
BRANCH_BLENDED = "blended"

CADENCE_LIGHT = "light"
CADENCE_MEDIUM = "medium"
CADENCE_HEAVY = "heavy"
_CADENCES = {CADENCE_LIGHT, CADENCE_MEDIUM, CADENCE_HEAVY}


# --- The questionnaire (rendered by the template; validated by the view) ----
# Each question: id, prompt, and 2-4 options. Every option carries the answer
# value(s) it sets. `allow_write_in` adds an optional text field. The whole
# thing is skippable — a user who skips lands on the baseline-3.
#
# Q2's options depend on Q1's branch, so the template shows the right set after
# Q1 is chosen; QUESTIONS lists every branch's Q2 variant keyed by branch.

QUESTIONS: List[dict] = [
    {
        "id": "q1_goal",
        "prompt": "What are you here for?",
        "allow_write_in": False,
        "options": [
            {"value": BRANCH_STRENGTH, "emoji": "💪", "label": "Getting stronger",
             "sub": "Lifting, muscle, performance"},
            {"value": BRANCH_HOLISTIC, "emoji": "🌱", "label": "Feeling better overall",
             "sub": "Energy, sleep, healthy habits"},
            {"value": BRANCH_BLENDED, "emoji": "⚖️", "label": "Both, honestly",
             "sub": "A bit of each"},
        ],
    },
    {
        "id": "q2_focus",
        "prompt": "Where do you most want to make progress?",
        "allow_write_in": True,
        # Branch-specific option sets — the template swaps these in after Q1.
        "branch_options": {
            BRANCH_STRENGTH: [
                {"value": "recovery", "label": "Recovery"},
                {"value": "nutrition", "label": "Nutrition"},
                {"value": "consistency", "label": "Consistency"},
                {"value": "mobility", "label": "Mobility"},
            ],
            BRANCH_HOLISTIC: [
                {"value": "sleep", "label": "Better sleep"},
                {"value": "movement", "label": "More movement"},
                {"value": "fuel", "label": "Hydration & food"},
                {"value": "calm", "label": "Stress & calm"},
            ],
            BRANCH_BLENDED: [
                {"value": "recovery", "label": "Recovery"},
                {"value": "nutrition", "label": "Nutrition"},
                {"value": "sleep", "label": "Better sleep"},
                {"value": "movement", "label": "More movement"},
            ],
        },
    },
    {
        "id": "q3_cadence",
        "prompt": "How much can you commit most days?",
        "allow_write_in": False,
        "options": [
            {"value": CADENCE_LIGHT, "label": "Just showing up", "sub": "Keep it light"},
            {"value": CADENCE_MEDIUM, "label": "A solid daily effort", "sub": "The usual"},
            {"value": CADENCE_HEAVY, "label": "All in", "sub": "Push me"},
        ],
    },
]


# --- The seeded top-3 library ----------------------------------------------
# Hand-written sets keyed by (branch, focus). Cadence tunes the quantitative
# targets via _scale_for_cadence so a "light" user isn't handed a "heavy" bar.
# Every set is exactly 3 items (keys stable, labels past-tense ≤60 chars).
# The FIRST item is the anchor/frog — the highest-leverage habit for that
# branch × focus; the other two support it. Three, chosen deliberately.

def _q(key: str, label: str) -> dict:
    return {"key": key, "label": label}


# Shared anchors reused across sets (the recognizable daily floor).
_WATER = _q("q_water", "Drank plenty of water")
_SLEEP = _q("q_sleep", "Slept 7+ hours")
_OUTSIDE = _q("q_outside", "Got outside / sunlight")
_MOVE = _q("q_move", "Moved my body 20+ minutes")
_WINS = _q("q_wins", "Noted a win for the day")

SEED_LIBRARY: Dict[Tuple[str, str], List[dict]] = {
    # --- Strength branch (frog = the training session) ---
    (BRANCH_STRENGTH, "recovery"): [
        _q("q_train", "Hit my training session"),
        _q("q_mobility", "Did 10 minutes of mobility"),
        _SLEEP,
    ],
    (BRANCH_STRENGTH, "nutrition"): [
        _q("q_train", "Hit my training session"),
        _q("q_protein", "Hit my protein target"),
        _WATER,
    ],
    (BRANCH_STRENGTH, "consistency"): [
        _q("q_train", "Hit my training session"),
        _q("q_protein", "Hit my protein target"),
        _WINS,
    ],
    (BRANCH_STRENGTH, "mobility"): [
        _q("q_train", "Hit my training session"),
        _q("q_mobility", "Did 10 minutes of mobility"),
        _SLEEP,
    ],
    # --- Holistic branch (frog = the focus habit) ---
    (BRANCH_HOLISTIC, "sleep"): [
        _SLEEP,
        _q("q_winddown", "Wound down screen-free before bed"),
        _WINS,
    ],
    (BRANCH_HOLISTIC, "movement"): [
        _MOVE,
        _OUTSIDE,
        _WINS,
    ],
    (BRANCH_HOLISTIC, "fuel"): [
        _q("q_nutrition", "Met my nutrition goal"),
        _WATER,
        _q("q_veg", "Ate vegetables with a meal"),
    ],
    (BRANCH_HOLISTIC, "calm"): [
        _q("q_calm", "Took 5 minutes to de-stress"),
        _OUTSIDE,
        _WINS,
    ],
    # --- Blended branch (frog = the workout) ---
    (BRANCH_BLENDED, "recovery"): [
        _q("q_train", "Got my workout in"),
        _q("q_mobility", "Did 10 minutes of mobility"),
        _SLEEP,
    ],
    (BRANCH_BLENDED, "nutrition"): [
        _q("q_train", "Got my workout in"),
        _q("q_nutrition", "Met my nutrition goal"),
        _WATER,
    ],
    (BRANCH_BLENDED, "sleep"): [
        _q("q_train", "Got my workout in"),
        _SLEEP,
        _WINS,
    ],
    (BRANCH_BLENDED, "movement"): [
        _MOVE,
        _q("q_train", "Got my workout in"),
        _WINS,
    ],
}

# Branch-level fallback when a focus has no exact set (or focus was skipped).
_BRANCH_FALLBACK: Dict[str, List[dict]] = {
    BRANCH_STRENGTH: SEED_LIBRARY[(BRANCH_STRENGTH, "consistency")],
    BRANCH_HOLISTIC: SEED_LIBRARY[(BRANCH_HOLISTIC, "movement")],
    BRANCH_BLENDED: SEED_LIBRARY[(BRANCH_BLENDED, "movement")],
}

# Absolute fallback (everything skipped) — the original Stronger-in-60 baseline
# lives in models.BASELINE_QUESTIONS; the view uses that directly, so here we
# just need a safe non-empty default.
_DEFAULT = _BRANCH_FALLBACK[BRANCH_BLENDED]


def _scale_for_cadence(items: List[dict], cadence: str) -> List[dict]:
    """Tune quantitative anchors to the user's stated daily floor. Light eases
    the most-demanding bars; heavy nudges them up. Conservative — we only touch
    a couple of well-known labels so we never produce something incoherent."""
    if cadence not in _CADENCES:
        return items
    out = []
    for it in items:
        label = it["label"]
        if cadence == CADENCE_LIGHT:
            label = label.replace("Slept 7+ hours", "Slept well")
            label = label.replace("Moved my body 20+ minutes", "Moved my body 10+ minutes")
        elif cadence == CADENCE_HEAVY:
            label = label.replace("Moved my body 20+ minutes", "Moved my body 45+ minutes")
            label = label.replace("Got my workout in", "Hit my training session")
        out.append({"key": it["key"], "label": label})
    return out


def seed_questions(branch: Optional[str], focus: Optional[str], cadence: Optional[str]) -> List[dict]:
    """Map onboarding answers → a 3-item seeded checklist. Tolerant of skips:
    a missing branch → blended default; a missing/unknown focus → the branch
    fallback; a missing cadence → no scaling. Always returns exactly 3 items."""
    from .ai_coach import CHECKLIST_SIZE
    branch = branch if branch in (BRANCH_STRENGTH, BRANCH_HOLISTIC, BRANCH_BLENDED) else BRANCH_BLENDED
    items = SEED_LIBRARY.get((branch, focus or "")) or _BRANCH_FALLBACK.get(branch) or _DEFAULT
    items = _scale_for_cadence(items, cadence or "")
    # Defensive: guarantee distinct-keyed items, capped at the core size.
    seen, result = set(), []
    for it in items:
        if it["key"] not in seen:
            seen.add(it["key"])
            result.append(it)
    return result[:CHECKLIST_SIZE]


def write_in_summary(write_ins: List[str]) -> str:
    """Join the optional free-text answers into a single line for the first
    coach context (so the overnight coach acts on the user's own words)."""
    cleaned = [w.strip() for w in (write_ins or []) if w and w.strip()]
    return " · ".join(cleaned)


# Human-readable labels for the coach note (the model reads plain language,
# not our internal slugs).
_BRANCH_LABEL = {
    BRANCH_STRENGTH: "getting stronger (strength focus)",
    BRANCH_HOLISTIC: "feeling better overall (holistic health)",
    BRANCH_BLENDED: "a blend of strength and holistic health",
}
_FOCUS_LABEL = {
    "recovery": "recovery", "nutrition": "nutrition", "consistency": "consistency",
    "mobility": "mobility", "sleep": "better sleep", "movement": "more movement",
    "fuel": "hydration & food", "calm": "stress & calm",
}
_CADENCE_LABEL = {
    CADENCE_LIGHT: "keeping it light most days",
    CADENCE_MEDIUM: "a solid daily effort",
    CADENCE_HEAVY: "going all in",
}


def survey_summary(branch, focus, cadence, write_ins: List[str]) -> str:
    """Render the onboarding answers (+ write-ins) as a short, natural coach
    note. This is the LIGHT STEER for users whose checklist comes from richer
    data (attestation history): the coach reads it as the user's stated intent
    and can lean that way over time, without it ever overwriting their list.
    Returns '' if the user answered nothing."""
    parts = []
    if branch in _BRANCH_LABEL:
        parts.append(f"In onboarding I said I'm here for {_BRANCH_LABEL[branch]}")
    if focus in _FOCUS_LABEL:
        parts.append(f"want to make progress on {_FOCUS_LABEL[focus]}")
    if cadence in _CADENCE_LABEL:
        parts.append(_CADENCE_LABEL[cadence])
    wi = write_in_summary(write_ins)
    base = ", ".join(parts)
    if base and wi:
        return f"{base}. Also noted: {wi}."
    if base:
        return base + "."
    return wi
