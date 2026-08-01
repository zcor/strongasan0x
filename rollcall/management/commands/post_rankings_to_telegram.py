"""Post the week's ranking to the Telegram group chat with the rendered
winner stanza as the photo.

Usage:
    python manage.py post_rankings_to_telegram --week-end 2026-05-10
    python manage.py post_rankings_to_telegram --week-end 2026-05-10 --dry-run

By default posts to the rollcall group chat. Override with --chat-id.

The post is a single sendPhoto: rankings + the on-site Roll Call link land in the photo's
caption so the winner stanza image sits at the top of the message.
"""
from __future__ import annotations

import os
from datetime import date
from html import escape, unescape
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from rollcall.models import (
    MessageLog,
    TelegramUserMapping,
    WeeklyRollCall,
)
from rollcall.services.winner_stanza import render_winner_image


# Hard-coded production target.
DEFAULT_TELEGRAM_CHAT_ID = -1003122619283
WEBSITE_URL = "https://strongasan0x.com"

RANK_EMOJI = {
    1: "🥇",
    2: "🥈",
    3: "🥉",
    4: "4️⃣",
    5: "5️⃣",
    6: "6️⃣",
    7: "7️⃣",
    8: "8️⃣",
    9: "9️⃣",
    10: "🔟",
}


def _ordinal_handle(rank: int, name: str, telegram_username: str | None) -> str:
    """Render one ranked line: emoji + @handle if known, else bare name."""
    prefix = RANK_EMOJI.get(rank, f"{rank}.")
    if telegram_username:
        return f"{prefix} @{_escape_html(telegram_username)}"
    return f"{prefix} {_escape_html(name)}"


def _escape_html(value: str) -> str:
    """Escape dynamic Telegram HTML exactly once at rendering time."""
    return escape(unescape(str(value)), quote=True)


def _format_date_range(week_start: date, week_end: date) -> str:
    """Return a compact human-readable week range."""
    if week_start.month == week_end.month:
        return (
            f"{week_start.strftime('%B')} {week_start.day} - "
            f"{week_end.day}, {week_end.year}"
        )
    return (
        f"{week_start.strftime('%b')} {week_start.day} - "
        f"{week_end.strftime('%b')} {week_end.day}, {week_end.year}"
    )


def _roll_call_url(roll_call: WeeklyRollCall) -> str:
    """Return the canonical on-site URL for a Roll Call."""
    return f"{WEBSITE_URL}/roll-call/{roll_call.week_end_date.isoformat()}/"


def _load_ranked_attestations(roll_call: WeeklyRollCall):
    """Return ranked list of (rank, Attestation, TelegramUserMapping) for the week.

    Pulls from RollCallRanking if present (post-ingest). Falls back to
    re-deriving from RankingTrial cumulative stats if rankings haven't
    been ingested yet.
    """
    from rollcall.models import RollCallRanking

    rankings = list(RollCallRanking.objects.filter(weekly_roll_call=roll_call).order_by("rank"))
    if rankings:
        result = []
        for r in rankings:
            mapping = _resolve_warrior_mapping(r.name)
            result.append((r.rank, r.name, mapping))
        return result

    # Fall back: derive from cumulative trial stats.
    from rollcall.services.ranking_stats import calculate_cumulative_stats
    from rollcall.models import RankingTrial

    trials = list(RankingTrial.objects.filter(weekly_roll_call=roll_call).order_by("trial_number"))
    if not trials:
        raise CommandError(
            f"No rankings and no ranking trials found for week starting "
            f"{roll_call.week_start_date}. Run run_ranking_trial first."
        )
    stats = calculate_cumulative_stats(trials)
    # stats is {name: {average_rank, std_error, trial_count, rankings}}
    sorted_stats = sorted(stats.items(), key=lambda kv: kv[1]["average_rank"])
    result = []
    for rank, (name, _entry) in enumerate(sorted_stats, start=1):
        result.append((rank, name, _resolve_warrior_mapping(name)))
    return result


def _resolve_warrior_mapping(name: str) -> TelegramUserMapping | None:
    """Find the TelegramUserMapping for a warrior name."""
    mapping = TelegramUserMapping.objects.filter(linked_name=name).first()
    if mapping:
        return mapping
    # Fuzzy: case-insensitive match.
    return TelegramUserMapping.objects.filter(linked_name__iexact=name).first()


def build_stats_message(roll_call: WeeklyRollCall) -> str | None:
    """Build the cumulative-trial-stats message in a monospace code block.

    Returns None if no trials exist for this roll call. Intentionally omits
    the model name and the convergence-framing — readers want warrior stats,
    not telemetry about our ranking infrastructure.
    """
    from rollcall.models import RankingTrial
    from rollcall.services.ranking_stats import calculate_cumulative_stats

    trials = list(
        RankingTrial.objects.filter(weekly_roll_call=roll_call).order_by("trial_number")
    )
    if not trials:
        return None
    stats = calculate_cumulative_stats(trials)
    sorted_stats = sorted(stats.items(), key=lambda x: x[1]["average_rank"])

    sep = "=" * 60
    mid = "-" * 60
    lines = [
        sep,
        "CUMULATIVE RANKINGS",
        sep,
        "",
        f"{'Rank':<6}{'Name':<22}{'Avg Rank':<14}{'Std Err':<10}{'Trials':<8}",
        mid,
    ]
    for rank, (name, s) in enumerate(sorted_stats, start=1):
        avg = f"{s['average_rank']:.2f}"
        se = f"± {s['std_error']:.2f}"
        safe_name = _escape_html(name)[:21]
        lines.append(f"{rank:<6}{safe_name:<22}{avg:<14}{se:<10}{s['trial_count']:<8}")
    lines.append(sep)
    table = "\n".join(lines)

    week_start = roll_call.week_start_date
    week_end = roll_call.week_end_date
    date_str = _format_date_range(week_start, week_end)
    return f"Ranking trial details - {date_str}\n<pre><code>{table}</code></pre>"


def build_caption(roll_call: WeeklyRollCall, ranked) -> str:
    week_start, week_end = roll_call.week_start_date, roll_call.week_end_date
    date_str = _format_date_range(week_start, week_end)
    lines = [f"🏆 🐂 Roll Call: {date_str}", "", "Ethereum's toughest warriors:", ""]
    for rank, name, mapping in ranked:
        lines.append(_ordinal_handle(rank, name, mapping.telegram_username if mapping else None))
    url = _roll_call_url(roll_call)
    lines.extend(["", "Full ode + details:", f'<a href="{url}">Roll Call</a>'])
    return "\n".join(lines)


def _find_ode_path(week_end: date) -> Path:
    """logs/<YYYY-MM-DD>/ode.md (preferred) or ode.txt fallback."""
    base = Path(settings.BASE_DIR) / "logs" / week_end.isoformat()
    for fname in ("ode.md", "ode.txt"):
        p = base / fname
        if p.exists():
            return p
    raise CommandError(f"No ode found in {base}/ — generate the ode first.")


class Command(BaseCommand):
    help = "Post weekly rankings + winner stanza image to Telegram"

    def add_arguments(self, parser):
        parser.add_argument(
            "--week-end",
            type=str,
            required=True,
            help="Week-end Sunday (YYYY-MM-DD)",
        )
        parser.add_argument(
            "--chat-id",
            type=int,
            default=DEFAULT_TELEGRAM_CHAT_ID,
            help=f"Target chat id (default {DEFAULT_TELEGRAM_CHAT_ID})",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be posted without sending",
        )

    def handle(self, *args, **options):
        week_end = date.fromisoformat(options["week_end"])
        if week_end.weekday() != 6:
            raise CommandError(f"{week_end} is not a Sunday")
        week_start = week_end - __import__("datetime").timedelta(days=6)

        roll_call = WeeklyRollCall.objects.filter(week_start_date=week_start).first()
        if not roll_call:
            raise CommandError(f"No WeeklyRollCall for week starting {week_start}")

        ranked = _load_ranked_attestations(roll_call)
        caption = build_caption(roll_call, ranked)

        ode_path = _find_ode_path(week_end)
        ode_text = ode_path.read_text()
        winner_png = ode_path.parent / "winner.png"
        render_winner_image(ode_text, winner_png)
        self.stdout.write(f"Rendered winner image: {winner_png}")

        self.stdout.write(self.style.NOTICE("=== CAPTION ==="))
        self.stdout.write(caption)
        self.stdout.write(f"caption length: {len(caption)} chars (Telegram limit 1024)")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — not posting"))
            return

        token = getattr(settings, "TELEGRAM_BOT_TOKEN", None) or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN not configured")

        chat_id = options["chat_id"]
        with open(winner_png, "rb") as photo:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": photo},
                timeout=30,
            )
        result = r.json()
        if not result.get("ok"):
            raise CommandError(f"Telegram send failed: {result}")
        msg = result["result"]
        self.stdout.write(self.style.SUCCESS(
            f"✅ Posted to Telegram: chat={msg['chat']['id']} message_id={msg['message_id']}"
        ))

        # Log to MessageLog for audit trail (best-effort)
        try:
            from datetime import datetime, timezone as tz
            MessageLog.objects.create(
                source="telegram",
                telegram_message_id=msg["message_id"],
                telegram_chat_id=chat_id,
                content=caption,
                posted_at=datetime.fromtimestamp(msg.get("date", 0), tz=tz.utc),
                is_bot_reply=True,
                kind="reply",
                has_attachments=True,
                attachment_count=1,
            )
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"MessageLog write failed (post still went out): {e}"))

        # Second message: cumulative trial stats inside a <code> block so the
        # Telegram client renders it in monospace with aligned columns. Posted
        # as a separate message rather than appended to the photo caption
        # because Telegram caption + photo + code block fights for layout.
        stats_msg = build_stats_message(roll_call)
        if stats_msg:
            r2 = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": stats_msg, "parse_mode": "HTML"},
                timeout=30,
            )
            j2 = r2.json()
            if j2.get("ok"):
                self.stdout.write(self.style.SUCCESS(
                    f"✅ Posted stats table: message_id={j2['result']['message_id']}"
                ))
            else:
                self.stdout.write(self.style.WARNING(f"Stats table post failed: {j2}"))
