"""
Wins backlog: the positive face of a put-off thing (see
daily/CLIMB_REFRAME_PLAN.md sections 2b / 2c). Beta-only.

The backlog is a pile of WinItem rows that is NEVER rendered all at once, or
even counted, on the daily surface. The user may explicitly choose any open
leaf; otherwise the first available leaf fills Today's Win automatically.
"Not today" returns it to the pile and advances to another leaf, never deletes it.
A win that starts sticking can graduate into a recurring habit.

Every function here scopes strictly to the passed participant — the single-user
authorization invariant (see plan section 8a). Callers must pass a participant
resolved from the session, never a client-supplied id.
"""
from __future__ import annotations

import logging
from datetime import date as date_cls, timedelta
from typing import List, Optional, Tuple

from django.db import transaction
from django.db.models import Case, IntegerField, Max, Q, Subquery, Value, When
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
    """Return the leaf currently selected as today's win.

    This getter does not mutate. The dashboard or a wins mutation fills an
    empty selection through ``select_next_todays_win``.
    """
    return participant.wins.filter(
        status=WinItem.STATUS_OPEN, is_goal=False, surfaced_on=today
    ).select_related("parent").order_by("order", "created_at").first()


def select_todays_win(
    participant: DailyParticipant, win: WinItem, today: date_cls
) -> WinItem:
    """Make ``win`` the participant's one explicit selection for ``today``."""
    with transaction.atomic():
        participant.wins.select_for_update().filter(
            status=WinItem.STATUS_OPEN, is_goal=False, surfaced_on=today
        ).exclude(id=win.id).update(surfaced_on=None)
        # Do not join the optional parent while locking. PostgreSQL rejects
        # FOR UPDATE on the nullable side of the LEFT OUTER JOIN, which made
        # selecting a standalone one-off fail even though no parent is needed
        # for this mutation.
        selected = participant.wins.select_for_update().get(
            id=win.id, status=WinItem.STATUS_OPEN, is_goal=False
        )
        selected.surfaced_on = today
        if selected.deferred_on == today:
            selected.deferred_on = None
            selected.save(update_fields=["surfaced_on", "deferred_on"])
        else:
            selected.save(update_fields=["surfaced_on"])
    return selected


def _selectable_wins(participant: DailyParticipant):
    """Open leaves that still belong to the active Your Wins view."""
    return participant.wins.filter(
        status=WinItem.STATUS_OPEN,
        is_goal=False,
    ).filter(
        Q(parent__isnull=True)
        | Q(parent__is_goal=True, parent__status=WinItem.STATUS_OPEN)
    )


def _ordered_selectable_wins(participant: DailyParticipant, today: date_cls):
    """Prefer a win not yet deferred today, then preserve the user's order."""
    return _selectable_wins(participant).annotate(
        deferred_today=Case(
            When(deferred_on=today, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
    ).order_by("deferred_today", "order", "created_at")


def select_next_todays_win(
    participant: DailyParticipant, today: date_cls,
) -> Optional[WinItem]:
    """Fill an empty Today's Win from the participant's own open list.

    Wins deferred today move behind the others. If every remaining win was
    deferred, the first one is selected again so a non-empty Your Wins list
    never leaves an empty daily card.
    """
    candidate = (
        _ordered_selectable_wins(participant, today)
        .select_related("parent")
        .first()
    )
    if candidate is None:
        return None
    select_todays_win(participant, candidate, today)
    # Keep the already-loaded parent cache used by the dashboard serializer.
    candidate.surfaced_on = today
    return candidate


def get_completed_todays_win(
    participant: DailyParticipant, today: date_cls
) -> Optional[WinItem]:
    """Return the most recently checked-off featured win for ``today``."""
    return participant.wins.filter(
        status=WinItem.STATUS_DONE,
        is_goal=False,
        surfaced_on=today,
    ).select_related("parent").order_by("-done_at", "-created_at").first()


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


def complete_win(
    win: WinItem, featured_on: Optional[date_cls] = None
) -> Tuple[WinItem, Optional[WinItem]]:
    """Check off a standalone win or north-star step.

    The parent north star deliberately remains open after its last step. The
    user finishes that group explicitly with ``complete_goal`` once every step
    is checked. The second tuple value remains for backwards-compatible callers
    and is always ``None``.
    """
    now = timezone.now()
    with transaction.atomic():
        win.status = WinItem.STATUS_DONE
        win.done_at = now
        # A featured completion retains the selection date. An arbitrary step
        # checked in the editor remains unsurfaced and never earns a week star.
        win.surfaced_on = featured_on
        win.save(update_fields=["status", "done_at", "surfaced_on"])
    return win, None


def uncomplete_win(win: WinItem, today: date_cls) -> WinItem:
    """Reopen a checked North Star step.

    If it was the selected win completed today, restore it to today's card.
    Older selection dates are cleared along with their completed state.
    """
    with transaction.atomic():
        restore_today = win.surfaced_on == today
        win.status = WinItem.STATUS_OPEN
        win.done_at = None
        win.surfaced_on = today if restore_today else None
        win.save(update_fields=["status", "done_at", "surfaced_on"])
    return win


def complete_goal(goal: WinItem) -> Optional[WinItem]:
    """Complete a north star once it has steps and none remain unchecked."""
    with transaction.atomic():
        locked = WinItem.objects.select_for_update().get(id=goal.id, is_goal=True)
        if locked.status != WinItem.STATUS_OPEN:
            return None
        stones = locked.stones.all()
        if not stones.exists() or stones.filter(status=WinItem.STATUS_OPEN).exists():
            return None
        locked.status = WinItem.STATUS_DONE
        locked.done_at = timezone.now()
        locked.surfaced_on = None
        locked.save(update_fields=["status", "done_at", "surfaced_on"])
    return locked


def archive_goal(goal: WinItem) -> WinItem:
    """Move an active North Star out of Working toward without deleting it."""
    with transaction.atomic():
        locked = WinItem.objects.select_for_update().get(
            id=goal.id, is_goal=True, status=WinItem.STATUS_OPEN
        )
        locked.status = WinItem.STATUS_ARCHIVED
        locked.done_at = None
        locked.surfaced_on = None
        locked.save(update_fields=["status", "done_at", "surfaced_on"])
        # An open selected step cannot remain on today's card after its parent
        # leaves active work. Completed steps retain their historical dates.
        locked.stones.filter(status=WinItem.STATUS_OPEN).update(surfaced_on=None)
    return locked


def restore_goal(goal: WinItem) -> WinItem:
    """Return an achieved or archived North Star to Working toward.

    Completed steps stay intact so reopening a North Star never erases the
    work that got it into history.
    """
    with transaction.atomic():
        locked = WinItem.objects.select_for_update().get(
            id=goal.id,
            is_goal=True,
            status__in=[WinItem.STATUS_DONE, WinItem.STATUS_ARCHIVED],
        )
        locked.status = WinItem.STATUS_OPEN
        locked.done_at = None
        locked.save(update_fields=["status", "done_at"])
    return locked


def create_north_star(
    participant: DailyParticipant, goal_text: str, stone_texts: List[str]
) -> Optional[WinItem]:
    """Create a north star (the big scope) that owns an ordered ladder of
    stepping stones. Returns the goal WinItem, or None on empty input / at cap.

    The goal itself is never surfaced; its stones are selectable leaves.
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
    if goal.stones.count() >= MAX_STONES_PER_GOAL:
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

    Removing a step never completes its parent. Even when no unchecked steps
    remain, the north star waits for the user's explicit Complete action.
    """
    with transaction.atomic():
        win.delete()
    return None


def list_backlog(participant: DailyParticipant):
    """Everything for the editor ("your list" door): north stars with their
    stones, plus standalone one-off wins. The ONLY place size/counts are shown.

    Returns open goals with all their steps (including crossed-off ones), plus
    open standalone wins.
    """
    # One query supplies the complete active editor plus the two group-status
    # flags. Avoid five WAN round trips (goals, stones, singles, achieved,
    # archived) when the web app is talking to managed PostgreSQL.
    rows = list(
        participant.wins.filter(
            Q(parent__isnull=True, status=WinItem.STATUS_OPEN)
            | Q(parent__status=WinItem.STATUS_OPEN)
            | Q(
                is_goal=True,
                status__in=[WinItem.STATUS_DONE, WinItem.STATUS_ARCHIVED],
            )
        )
        .select_related("parent")
        .order_by("order", "created_at")
    )
    goals = [
        row for row in rows
        if row.is_goal and row.status == WinItem.STATUS_OPEN
    ]
    stones_by_goal = {g.id: [] for g in goals}
    for stone in rows:
        if stone.parent_id in stones_by_goal:
            stones_by_goal[stone.parent_id].append(stone)
    singles = [
        row for row in rows
        if (
            row.status == WinItem.STATUS_OPEN
            and not row.is_goal
            and row.parent_id is None
        )
    ]
    return {
        "goals": [
            {
                "goal": g,
                "stones": stones_by_goal[g.id],
                "open_count": sum(
                    s.status == WinItem.STATUS_OPEN for s in stones_by_goal[g.id]
                ),
                "can_complete": bool(stones_by_goal[g.id]) and all(
                    s.status != WinItem.STATUS_OPEN for s in stones_by_goal[g.id]
                ),
            }
            for g in goals
        ],
        "singles": singles,
        "has_achieved": any(
            row.is_goal and row.status == WinItem.STATUS_DONE for row in rows
        ),
        "has_archived": any(
            row.is_goal and row.status == WinItem.STATUS_ARCHIVED for row in rows
        ),
    }


def get_dashboard_wins(
    participant: DailyParticipant,
    start: date_cls,
    today: date_cls,
    auto_select: bool = True,
):
    """Load the week-strip win markers and today's card state in one query.

    The full backlog belongs to the lazy wins dialog and is intentionally not
    pulled into the dashboard's critical render path.
    """
    card_rows = Q(
        status=WinItem.STATUS_OPEN,
        is_goal=False,
        surfaced_on=today,
    )
    if auto_select:
        # Include only the first possible fallback in the same dashboard query.
        # This avoids loading the full Wins backlog or adding a query on empty
        # accounts, while still letting us persist a selection when one exists.
        candidate_id = Subquery(
            _ordered_selectable_wins(participant, today).values("id")[:1]
        )
        card_rows |= Q(id=candidate_id)
    rows = list(
        participant.wins.filter(
            card_rows
            | Q(
                status=WinItem.STATUS_DONE,
                is_goal=False,
                surfaced_on__gte=start,
                surfaced_on__lte=today,
            )
        )
        .select_related("parent")
        .order_by("order", "created_at")
    )
    selected = next(
        (
            row for row in rows
            if row.status == WinItem.STATUS_OPEN and row.surfaced_on == today
        ),
        None,
    )
    completed_rows = [
        row
        for row in rows
        if row.status == WinItem.STATUS_DONE and row.surfaced_on == today
    ]
    completed = max(
        completed_rows,
        key=lambda row: (row.done_at is not None, row.done_at, row.created_at),
        default=None,
    )
    if auto_select and selected is None and completed is None:
        candidate = next(
            (row for row in rows if row.status == WinItem.STATUS_OPEN),
            None,
        )
        if candidate is not None:
            select_todays_win(participant, candidate, today)
            candidate.surfaced_on = today
            selected = candidate
    return {
        "selected": selected,
        "completed": completed,
        "done_dates": {
            row.surfaced_on
            for row in rows
            if row.status == WinItem.STATUS_DONE and row.surfaced_on is not None
        },
    }


def todays_win_done_dates(
    participant: DailyParticipant, start: date_cls, end: date_cls
):
    """Dates whose explicit Today's Win was checked off."""
    return set(participant.wins.filter(
        is_goal=False,
        status=WinItem.STATUS_DONE,
        surfaced_on__gte=start,
        surfaced_on__lte=end,
    ).values_list("surfaced_on", flat=True))


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
    """"Not today": return the selected item to the pile for reselection."""
    win.surfaced_on = None
    win.deferred_on = today
    win.defer_count = (win.defer_count or 0) + 1
    win.save(update_fields=["surfaced_on", "deferred_on", "defer_count"])
    return win


def promote_to_habit(win: WinItem, participant: DailyParticipant):
    """A standalone one-off win that has started sticking graduates into a
    recurring habit (plan section 1a). Appends it to the current checklist and
    marks the win graduated. North Star steps never graduate; the endpoint
    only routes standalone wins here. Returns (item, None), or (None, None) if
    the checklist is full. Caller owns the label -> habit wording.
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
    return item, None
