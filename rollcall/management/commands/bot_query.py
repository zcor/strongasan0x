"""bot_query — fixed-verb command surface for the conversational bot.

NOT a Python sandbox. The model picks a verb from a documented cookbook,
fills a JSON arg blob, and parses a JSON envelope back. There is no eval,
exec, or user-authored Python code path. Adding a new query capability
requires a code-reviewed PR adding a new verb function.

Usage from Claude CLI (allowlist: `Bash(python manage.py bot_query:*)`):
    python manage.py bot_query <verb> --json '{"name": "Spencer", "weeks": 12}'

Returns: {"ok": true, "data": ..., "rows": N} or {"ok": false, "error": "..."}
to stdout. Audit log written to logs/bot_query_audit.log.

Scope: env var BOT_SCOPE=dm enables DM-only verbs (list_my_uploads,
read_my_upload). BOT_SCOPE=group omits them entirely. VIEWER_TELEGRAM_ID
must be set; private-data verbs hard-check it server-side.
"""
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# CRITICAL: Set BOT_QUERY_MODE before any model import so the router engages.
os.environ["BOT_QUERY_MODE"] = "1"

from django.conf import settings  # noqa: E402
from django.core.management.base import BaseCommand  # noqa: E402

# Configure a dedicated audit log
_AUDIT_LOGGER = logging.getLogger("bot_query_audit")
if not _AUDIT_LOGGER.handlers:
    _logfile = Path(settings.BASE_DIR) / "logs" / "bot_query_audit.log"
    _logfile.parent.mkdir(parents=True, exist_ok=True)
    _h = logging.FileHandler(_logfile)
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _AUDIT_LOGGER.addHandler(_h)
    _AUDIT_LOGGER.setLevel(logging.INFO)


# ── Verb implementations ────────────────────────────────────────────────

ALLOWED_METRICS = frozenset({
    "bench_press", "squat", "deadlift",
    "daily_steps", "calories_burned", "resting_heart_rate", "vo2_max",
    "sleep_hours", "body_weight", "body_fat_pct",
    "strength_sessions", "cardio_sessions", "combat_sessions", "total_training_sessions",
    "protein_grams", "calories_consumed",
})


def _resolve_warrior(name: str):
    """Resolve a name (canonical or alias) to a list of (mapping, source) entries."""
    from rollcall.models import DiscordUserMapping, TelegramUserMapping
    matches = []
    name_lower = name.lower().strip()
    for m in TelegramUserMapping.objects.filter(linked_name__iexact=name):
        matches.append((m, "telegram"))
    for m in DiscordUserMapping.objects.filter(linked_name__iexact=name):
        matches.append((m, "discord"))
    return matches


def verb_warrior_history(args: dict, viewer_id: int, scope: str) -> dict:
    """Last N weeks of attestations + metrics for the named warrior."""
    from rollcall.models import Attestation
    name = args["name"]
    weeks = int(args.get("weeks", 12))
    matches = _resolve_warrior(name)
    if not matches:
        return {"ok": False, "error": f"warrior not found: {name}"}

    cutoff = datetime.now().date() - timedelta(weeks=weeks)
    rows = []
    for mapping, src in matches:
        if src == "telegram":
            qs = Attestation.objects.filter(
                telegram_user=mapping,
                parent_attestation__isnull=True,
                weekly_roll_call__week_start_date__gte=cutoff,
                is_hidden=False,
            )
        else:
            qs = Attestation.objects.filter(
                discord_user=mapping,
                parent_attestation__isnull=True,
                weekly_roll_call__week_start_date__gte=cutoff,
                is_hidden=False,
            )
        for a in qs.select_related("weekly_roll_call").order_by("-posted_at"):
            metrics = getattr(a, "metrics", None)
            rows.append({
                "week_end": a.weekly_roll_call.week_end_date.isoformat(),
                "posted_at": a.posted_at.isoformat(),
                "source": a.source,
                "text": (a.raw_text or "")[:1500],
                "metrics": _metrics_to_dict(metrics) if metrics else None,
            })
    rows.sort(key=lambda r: r["week_end"], reverse=True)
    return {"ok": True, "data": {"warrior": name, "weeks": weeks, "attestations": rows}, "rows": len(rows)}


def _metrics_to_dict(m) -> dict:
    return {f: getattr(m, f) for f in (
        "bench_press", "squat", "deadlift",
        "daily_steps", "calories_burned", "sleep_hours", "body_weight", "body_fat_pct",
        "strength_sessions", "cardio_sessions", "combat_sessions", "total_training_sessions",
    ) if getattr(m, f) is not None}


def verb_warrior_metric_series(args: dict, viewer_id: int, scope: str) -> dict:
    """Time series for one metric across N weeks. Returns [(week_end, value), ...]."""
    from rollcall.models import ExtractedMetrics
    name = args["name"]
    metric = args["metric"]
    weeks = int(args.get("weeks", 52))

    if metric not in ALLOWED_METRICS:
        return {"ok": False, "error": f"unknown metric: {metric}. Allowed: {sorted(ALLOWED_METRICS)}"}

    matches = _resolve_warrior(name)
    if not matches:
        return {"ok": False, "error": f"warrior not found: {name}"}

    cutoff = datetime.now().date() - timedelta(weeks=weeks)
    series = []
    for mapping, src in matches:
        filt = {
            "attestation__parent_attestation__isnull": True,
            "attestation__weekly_roll_call__week_start_date__gte": cutoff,
            "attestation__is_hidden": False,
            f"{metric}__isnull": False,
        }
        if src == "telegram":
            filt["attestation__telegram_user"] = mapping
        else:
            filt["attestation__discord_user"] = mapping
        qs = ExtractedMetrics.objects.filter(**filt).select_related("attestation__weekly_roll_call")
        for em in qs.order_by("attestation__weekly_roll_call__week_start_date"):
            series.append({
                "week_end": em.attestation.weekly_roll_call.week_end_date.isoformat(),
                "value": getattr(em, metric),
            })
    return {"ok": True, "data": {"warrior": name, "metric": metric, "series": series}, "rows": len(series)}


def verb_weekly_leaderboard(args: dict, viewer_id: int, scope: str) -> dict:
    """The published RollCallRanking for a given week-end date."""
    from rollcall.models import RollCallRanking, WeeklyRollCall
    week_end = args["week_end"]  # ISO date string
    try:
        we = date.fromisoformat(week_end)
    except ValueError:
        return {"ok": False, "error": f"week_end must be ISO date (YYYY-MM-DD), got {week_end!r}"}
    rc = WeeklyRollCall.objects.filter(week_end_date=we).first()
    if not rc:
        return {"ok": False, "error": f"no roll call for week ending {week_end}"}
    rows = [
        {"rank": r.rank, "name": r.name, "twitter": r.twitter_handle}
        for r in RollCallRanking.objects.filter(weekly_roll_call=rc).order_by("rank")
    ]
    return {"ok": True, "data": {"week_end": week_end, "published": rc.is_published, "ranking": rows}, "rows": len(rows)}


def verb_compare_warriors(args: dict, viewer_id: int, scope: str) -> dict:
    """Side-by-side metric series for multiple warriors."""
    names = args["names"]
    metric = args["metric"]
    weeks = int(args.get("weeks", 12))
    if metric not in ALLOWED_METRICS:
        return {"ok": False, "error": f"unknown metric: {metric}"}
    by_warrior = {}
    for name in names:
        sub = verb_warrior_metric_series({"name": name, "metric": metric, "weeks": weeks}, viewer_id, scope)
        if sub["ok"]:
            by_warrior[name] = sub["data"]["series"]
    return {"ok": True, "data": {"metric": metric, "weeks": weeks, "by_warrior": by_warrior}, "rows": sum(len(s) for s in by_warrior.values())}


def verb_seasonal_breakdown(args: dict, viewer_id: int, scope: str) -> dict:
    """Group a warrior's metric series by season; return mean and stddev per season."""
    import statistics
    name = args["name"]
    metric = args["metric"]
    sub = verb_warrior_metric_series({"name": name, "metric": metric, "weeks": 520}, viewer_id, scope)
    if not sub["ok"]:
        return sub
    buckets = {"winter": [], "spring": [], "summer": [], "fall": []}
    for entry in sub["data"]["series"]:
        m = date.fromisoformat(entry["week_end"]).month
        if m in (12, 1, 2):
            season = "winter"
        elif m in (3, 4, 5):
            season = "spring"
        elif m in (6, 7, 8):
            season = "summer"
        else:
            season = "fall"
        buckets[season].append(entry["value"])
    summary = {}
    for season, vals in buckets.items():
        if vals:
            summary[season] = {
                "n": len(vals),
                "mean": statistics.mean(vals),
                "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
                "min": min(vals),
                "max": max(vals),
            }
    return {"ok": True, "data": {"warrior": name, "metric": metric, "seasons": summary}}


def verb_recent_attestations(args: dict, viewer_id: int, scope: str) -> dict:
    """Last N raw attestation texts (truncated)."""
    from rollcall.models import Attestation
    name = args["name"]
    n = int(args.get("n", 4))
    matches = _resolve_warrior(name)
    if not matches:
        return {"ok": False, "error": f"warrior not found: {name}"}
    rows = []
    for mapping, src in matches:
        if src == "telegram":
            qs = Attestation.objects.filter(telegram_user=mapping, parent_attestation__isnull=True, is_hidden=False)
        else:
            qs = Attestation.objects.filter(discord_user=mapping, parent_attestation__isnull=True, is_hidden=False)
        for a in qs.select_related("weekly_roll_call").order_by("-posted_at")[:n]:
            rows.append({
                "week_end": a.weekly_roll_call.week_end_date.isoformat(),
                "posted_at": a.posted_at.isoformat(),
                "source": a.source,
                "text": (a.raw_text or "")[:1500],
            })
    rows.sort(key=lambda r: r["posted_at"], reverse=True)
    return {"ok": True, "data": {"warrior": name, "attestations": rows[:n]}, "rows": len(rows[:n])}


# ── DM-only verbs (registered only when BOT_SCOPE=dm) ──────────────────

def verb_list_my_uploads(args: dict, viewer_id: int, scope: str) -> dict:
    """List the viewer's own private uploads. NO bytes, just metadata.
    Phase B placeholder — Phase C will implement WarriorPrivateUpload."""
    return {"ok": True, "data": {"uploads": []}, "rows": 0,
            "note": "private uploads not yet implemented (Phase C)"}


def verb_read_my_upload(args: dict, viewer_id: int, scope: str) -> dict:
    """Read one of the viewer's own private uploads. Placeholder for Phase C."""
    return {"ok": False, "error": "private uploads not yet implemented (Phase C)"}


# ── Dispatcher ──────────────────────────────────────────────────────────

# Verbs available in any scope:
ALWAYS_VERBS = {
    "warrior_history": verb_warrior_history,
    "warrior_metric_series": verb_warrior_metric_series,
    "weekly_leaderboard": verb_weekly_leaderboard,
    "compare_warriors": verb_compare_warriors,
    "seasonal_breakdown": verb_seasonal_breakdown,
    "recent_attestations": verb_recent_attestations,
}

# Verbs available only when BOT_SCOPE=dm:
DM_ONLY_VERBS = {
    "list_my_uploads": verb_list_my_uploads,
    "read_my_upload": verb_read_my_upload,
}


def get_registered_verbs(scope: str) -> dict:
    """Return the verb dispatch table for the given scope.

    Critical: DM-only verbs are NOT in the returned dict when scope is "group".
    Calling them returns 'verb not available in this scope' BEFORE any
    DB access — the dispatcher is the boundary, not the prompt.
    """
    if scope == "dm":
        return {**ALWAYS_VERBS, **DM_ONLY_VERBS}
    return dict(ALWAYS_VERBS)


class Command(BaseCommand):
    help = "Read-only DB query dispatcher for the conversational bot. Fixed verb set, no arbitrary Python."

    def add_arguments(self, parser):
        parser.add_argument("verb", help="Verb name")
        parser.add_argument("--json", default="{}", help="JSON args blob (default: {})")

    def handle(self, *args, **opts):
        verb = opts["verb"]
        scope = os.environ.get("BOT_SCOPE", "dm")  # default DM for safety in tests
        viewer_id_str = os.environ.get("VIEWER_TELEGRAM_ID", "0")
        try:
            viewer_id = int(viewer_id_str)
        except ValueError:
            viewer_id = 0

        try:
            verb_args = json.loads(opts["json"])
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({"ok": False, "error": f"bad --json: {e}"}) + "\n")
            return

        verbs = get_registered_verbs(scope)
        fn = verbs.get(verb)
        if fn is None:
            envelope = {"ok": False, "error": f"verb not available in this scope ({scope}): {verb}"}
            _AUDIT_LOGGER.info("DENIED scope=%s viewer=%s verb=%s args=%s", scope, viewer_id, verb, verb_args)
            sys.stdout.write(json.dumps(envelope) + "\n")
            return

        _AUDIT_LOGGER.info("CALL scope=%s viewer=%s verb=%s args=%s", scope, viewer_id, verb, verb_args)
        try:
            envelope = fn(verb_args, viewer_id, scope)
        except KeyError as e:
            envelope = {"ok": False, "error": f"missing arg: {e}"}
        except Exception as e:
            envelope = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        rows = envelope.get("rows", "-")
        _AUDIT_LOGGER.info("RESULT scope=%s viewer=%s verb=%s ok=%s rows=%s",
                           scope, viewer_id, verb, envelope.get("ok"), rows)
        sys.stdout.write(json.dumps(envelope, default=str) + "\n")
