"""
Wins backlog: the positive face of a put-off thing (see
daily/CLIMB_REFRAME_PLAN.md sections 2b / 2c). Beta-only.

The backlog is a pile of WinItem rows that is NEVER rendered all at once, or
even counted, on the daily surface. Exactly ONE open item is "surfaced" as
today's win at a time (surface-one). "Not today" defers (back to the pile,
surface a different one), never deletes. A win that starts sticking can
graduate into a recurring habit.

Every function here scopes strictly to the passed participant — the single-user
authorization invariant (see plan section 8a). Callers must pass a participant
resolved from the session, never a client-supplied id.
"""
from __future__ import annotations

import logging
from datetime import date as date_cls, timedelta
from typing import List, Optional, Tuple

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ..models import DailyParticipant, WinItem

# A north star may hold a generous but bounded ladder of stones. Never counted
# to the user on the daily surface; this only guards a runaway client.
MAX_STONES_PER_GOAL = 30

logger = logging.getLogger(__name__)

# Generous ceiling on the pile. Never surfaced or counted to the user; this is
# only a runaway guard so a broken client can't create rows without bound.
MAX_WINS_BACKLOG = 200


def get_todays_win(participant: DailyParticipant, today: date_cls) -> Optional[WinItem]:
    """Return the WinItem surfaced as today's win, surfacing the next eligible
    one if none is yet. Resting state is strictly ONE item (or None when the
    pile is empty or everything open was deferred today). Idempotent per day.

    Selection rule (v1): user-ordered — the lowest-order open item not already
    deferred today. Jamie suggests but does not impose; a swap ("Not today")
    cycles to the next.

    Only LEAVES are ever surfaced: a standalone win or a north star's next open
    stepping stone. A north star (is_goal=True) is never surfaced on its own; it
    completes when its last stone is done (see complete_win).
    """
    surfaced = participant.wins.filter(
        status=WinItem.STATUS_OPEN, is_goal=False, surfaced_on=today
    ).select_related("parent").order_by("order", "created_at").first()
    if surfaced is not None:
        return surfaced

    with transaction.atomic():
        nxt = (
            participant.wins.select_for_update(of=("self",))
            .select_related("parent")
            .filter(status=WinItem.STATUS_OPEN, is_goal=False)
            .exclude(deferred_on=today)
            .order_by("order", "created_at")
            .first()
        )
        if nxt is None:
            return None
        nxt.surfaced_on = today
        nxt.save(update_fields=["surfaced_on"])
        return nxt


def add_win(participant: DailyParticipant, text: str) -> Optional[WinItem]:
    """Add a new win to the pile (user's own words). Appended to the end of the
    user's ordering. Returns the created WinItem, or None if empty / at cap."""
    text = (text or "").strip()[:200]
    if not text:
        return None
    open_count = participant.wins.filter(status=WinItem.STATUS_OPEN).count()
    if open_count >= MAX_WINS_BACKLOG:
        logger.warning("daily.wins: backlog at cap for %s", participant)
        return None
    next_order = (
        participant.wins.aggregate(m=Max("order")).get("m")
    )
    next_order = 0 if next_order is None else next_order + 1
    return WinItem.objects.create(
        participant=participant, text=text, order=next_order,
    )


def complete_win(win: WinItem) -> Tuple[WinItem, Optional[WinItem]]:
    """Mark a win done. This is the whole point — a put-off thing knocked down.

    Returns (win, completed_goal). When the win is the LAST open stone of a
    north star, that north star is completed too and returned as the second
    element (else None) — the summit moment the daily surface celebrates and the
    week strip stars.
    """
    now = timezone.now()
    with transaction.atomic():
        win.status = WinItem.STATUS_DONE
        win.done_at = now
        win.surfaced_on = None
        win.save(update_fields=["status", "done_at", "surfaced_on"])
        completed_goal = _maybe_complete_parent(win, now)
    return win, completed_goal


def _maybe_complete_parent(win: WinItem, now) -> Optional[WinItem]:
    """If `win` was the LAST open stone of its north star, complete the star
    and return it (else None). Must run inside the transaction that already
    closed `win` (done OR graduated) — every path that removes a stone from
    the open set (complete_win, promote_to_habit) must call this, or the
    goal is stranded permanently OPEN with nothing left to surface."""
    if not win.parent_id:
        return None
    goal = WinItem.objects.select_for_update().get(id=win.parent_id)
    more_open = goal.stones.filter(status=WinItem.STATUS_OPEN).exists()
    if goal.status != WinItem.STATUS_OPEN or more_open:
        return None
    goal.status = WinItem.STATUS_DONE
    goal.done_at = now
    goal.surfaced_on = None
    goal.save(update_fields=["status", "done_at", "surfaced_on"])
    return goal


def create_north_star(
    participant: DailyParticipant, goal_text: str, stone_texts: List[str]
) -> Optional[WinItem]:
    """Create a north star (the big scope) that owns an ordered ladder of
    stepping stones. Returns the goal WinItem, or None on empty input / at cap.

    The goal itself is never surfaced; its stones are appended to the pile like
    ordinary leaves, so surface-one walks them one per day.
    """
    goal_text = (goal_text or "").strip()[:200]
    stones = [s.strip()[:200] for s in (stone_texts or []) if s and s.strip()]
    stones = stones[:MAX_STONES_PER_GOAL]
    if not goal_text or not stones:
        return None
    open_count = participant.wins.filter(status=WinItem.STATUS_OPEN).count()
    if open_count + len(stones) > MAX_WINS_BACKLOG:
        logger.warning("daily.wins: backlog cap would be exceeded for %s", participant)
        return None
    with transaction.atomic():
        base = participant.wins.aggregate(m=Max("order")).get("m")
        base = 0 if base is None else base + 1
        goal = WinItem.objects.create(
            participant=participant, text=goal_text, is_goal=True, order=base,
        )
        for i, text in enumerate(stones):
            WinItem.objects.create(
                participant=participant, text=text, parent=goal, order=base + 1 + i,
            )
    return goal


def add_stone(participant: DailyParticipant, goal: WinItem, text: str) -> Optional[WinItem]:
    """Append a stepping stone to an existing north star. Returns the new stone,
    or None on empty input / at cap. If the goal had been completed, adding a
    fresh stone reopens it (the summit moved)."""
    text = (text or "").strip()[:200]
    if not text or not goal.is_goal:
        return None
    if goal.stones.filter(status=WinItem.STATUS_OPEN).count() >= MAX_STONES_PER_GOAL:
        logger.warning("daily.wins: stone cap reached on goal %s", goal.id)
        return None
    with transaction.atomic():
        next_order = participant.wins.aggregate(m=Max("order")).get("m")
        next_order = 0 if next_order is None else next_order + 1
        stone = WinItem.objects.create(
            participant=participant, text=text, parent=goal, order=next_order,
        )
        if goal.status == WinItem.STATUS_DONE:
            goal.status = WinItem.STATUS_OPEN
            goal.done_at = None
            goal.save(update_fields=["status", "done_at"])
    return stone


def remove_win(win: WinItem) -> Optional[WinItem]:
    """Delete a win. Removing a north star cascades to its stones (FK CASCADE).

    Deleting a STONE removes it from the open set, so the parent goal must be
    re-checked (the _maybe_complete_parent invariant) — but only a goal with
    at least one done/graduated stone completes; deleting every stone of an
    untouched goal leaves it OPEN (the user can add fresh stones in the
    editor), it doesn't fake an achievement. Returns the completed goal or
    None, same contract as complete_win's second element.
    """
    with transaction.atomic():
        win.delete()  # clears pk; parent_id survives for the check below
        if not win.parent_id:
            return None
        had_progress = WinItem.objects.filter(parent_id=win.parent_id).exclude(
            status=WinItem.STATUS_OPEN
        ).exists()
        if not had_progress:
            return None
        return _maybe_complete_parent(win, timezone.now())


def list_backlog(participant: DailyParticipant):
    """Everything for the editor ("your list" door): north stars with their
    stones, plus standalone one-off wins. The ONLY place size/counts are shown.

    Returns {"goals": [{"goal": WinItem, "stones": [WinItem, ...]}, ...],
             "singles": [WinItem, ...]} — open items only.
    """
    open_wins = list(
        participant.wins.filter(status=WinItem.STATUS_OPEN).order_by("order", "created_at")
    )
    goals = [w for w in open_wins if w.is_goal]
    stones_by_goal = {}
    singles = []
    for w in open_wins:
        if w.is_goal:
            continue
        if w.parent_id:
            stones_by_goal.setdefault(w.parent_id, []).append(w)
        else:
            singles.append(w)
    return {
        "goals": [{"goal": g, "stones": stones_by_goal.get(g.id, [])} for g in goals],
        "singles": singles,
    }


def north_star_done_dates(participant: DailyParticipant, start: date_cls, end: date_cls):
    """The set of the participant's LOCAL dates in [start, end] on which a north
    star was completed — one gold star per such day in the week strip. Uses the
    participant's own timezone so the star lands on the right day at the edges."""
    from .tz import participant_tz

    tz = participant_tz(participant)
    # Bound the scan in UTC with a day of slack on each side; the exact
    # per-timezone day is still decided in Python below.
    done = participant.wins.filter(
        is_goal=True, status=WinItem.STATUS_DONE,
        done_at__date__gte=start - timedelta(days=1),
        done_at__date__lte=end + timedelta(days=1),
    ).values_list("done_at", flat=True)
    days = set()
    for dt in done:
        d = timezone.localtime(dt, tz).date()
        if start <= d <= end:
            days.add(d)
    return days


def defer_win(win: WinItem, today: date_cls) -> WinItem:
    """"Not today": send the surfaced item back to the pile and mark it deferred
    today so surface-one picks a different one. Never punished, never deleted."""
    win.surfaced_on = None
    win.deferred_on = today
    win.defer_count = (win.defer_count or 0) + 1
    win.save(update_fields=["surfaced_on", "deferred_on", "defer_count"])
    return win


def promote_to_habit(win: WinItem, participant: DailyParticipant):
    """A win that has started sticking graduates into a recurring habit (plan
    section 1a). Appends it to the current checklist and marks the win
    graduated. Returns (item, completed_goal): the new habit item dict plus
    the win's north star if graduating its last open stone completed it
    (same summit semantics as complete_win). (None, None) if the checklist
    is full. Caller owns the label -> habit wording.
    """
    from .checklist import MAX_CHECKLIST_SIZE, all_version_keys
    from ..models import ChecklistVersion
    import uuid

    version = participant.get_or_create_current_checklist()
    if len(version.questions) >= MAX_CHECKLIST_SIZE:
        return None, None
    item = {"key": "w_" + uuid.uuid4().hex[:8], "label": win.text[:60]}
    with transaction.atomic():
        v = ChecklistVersion.objects.select_for_update().get(id=version.id)
        if item["key"] in all_version_keys(v):
            return None, None
        v.questions = list(v.questions) + [item]
        v.save(update_fields=["questions"])
        win.status = WinItem.STATUS_GRADUATED
        win.surfaced_on = None
        win.save(update_fields=["status", "surfaced_on"])
        completed_goal = _maybe_complete_parent(win, timezone.now())
    return item, completed_goal
