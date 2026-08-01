"""Post the week's ranking thread to 𝕏.

Usage:
    python manage.py post_rankings_to_x --week-end 2026-05-10
    python manage.py post_rankings_to_x --week-end 2026-05-10 --dry-run

Tweet 1 carries the rankings + the rendered winner stanza image.
A 60-second sleep follows, then tweet 2 (a reply) carries the
recruitment+info block with Roll Call, form, Discord, and website links.

If tweet 2 exceeds the 𝕏 per-tweet length limit, the command retries
with a pre-defined trimmed variant. Tweet 1 is always sent first; if
tweet 2 fails entirely, tweet 1 stays live and the URL is logged.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from rollcall.models import RollCallRanking, TelegramUserMapping, WeeklyRollCall
from rollcall.services.winner_stanza import render_winner_image


FORMS_URL = "https://forms.gle/pwBvd15SmsjDPCfK7"
DISCORD_URL = "https://discord.gg/2wQpAHme3R"
WEBSITE_URL = "https://strongasan0x.com"

SLEEP_BETWEEN_TWEETS_SECONDS = 60

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


def _format_x_date_range(week_start: date, week_end: date) -> str:
    """Historical 𝕏 format: 'May. 4 - May. 10, 2026'."""
    if week_start.month == week_end.month:
        return (
            f"{week_start.strftime('%b')}. {week_start.day} - "
            f"{week_start.strftime('%b')}. {week_end.day}, {week_end.year}"
        )
    return (
        f"{week_start.strftime('%b')}. {week_start.day} - "
        f"{week_end.strftime('%b')}. {week_end.day}, {week_end.year}"
    )


def _x_handle(name: str) -> str | None:
    """Look up the warrior's stored 𝕏 (linked_twitter_handle) for the given name."""
    mapping = (
        TelegramUserMapping.objects.filter(linked_name=name).first()
        or TelegramUserMapping.objects.filter(linked_name__iexact=name).first()
    )
    if mapping and mapping.linked_twitter_handle:
        return mapping.linked_twitter_handle.lstrip("@")
    return None


def _load_ranked(roll_call: WeeklyRollCall):
    rankings = list(
        RollCallRanking.objects.filter(weekly_roll_call=roll_call).order_by("rank")
    )
    if rankings:
        return [(r.rank, r.name) for r in rankings]

    from rollcall.models import RankingTrial
    from rollcall.services.ranking_stats import calculate_cumulative_stats

    trials = list(
        RankingTrial.objects.filter(weekly_roll_call=roll_call).order_by("trial_number")
    )
    if not trials:
        raise CommandError(
            f"No rankings/trials for week starting {roll_call.week_start_date}"
        )
    stats = calculate_cumulative_stats(trials)
    sorted_stats = sorted(stats.items(), key=lambda kv: kv[1]["average_rank"])
    return [(rank, name) for rank, (name, _entry) in enumerate(sorted_stats, start=1)]


def build_tweet1(roll_call: WeeklyRollCall, ranked) -> str:
    date_str = _format_x_date_range(roll_call.week_start_date, roll_call.week_end_date)
    lines = [
        "Are you Strong as an 0x? 🏋️ 🐂",
        "Ethereum's toughest warriors",
        date_str,
        "",
    ]
    for rank, name in ranked:
        handle = _x_handle(name)
        prefix = RANK_EMOJI.get(rank, f"{rank}.")
        lines.append(f"{prefix} @{handle}" if handle else f"{prefix} {name}")
    return "\n".join(lines)


def _roll_call_url(roll_call: WeeklyRollCall) -> str:
    """Return the canonical on-site URL for a Roll Call."""
    return f"{WEBSITE_URL}/roll-call/{roll_call.week_end_date.isoformat()}/"


def build_tweet2_full(roll_call: WeeklyRollCall) -> str:
    roll_call_url = _roll_call_url(roll_call)
    return (
        f"Are you strong as an 0x?   We invite recruits to submit a weekly health attestation:  {FORMS_URL}\n"
        f"\n"
        f"We feed it to the oracle and publish the top ten each week: {roll_call_url}\n"
        f"\n"
        f"MORE INFO\n"
        f"  Discord: {DISCORD_URL}\n"
        f"  Website: {WEBSITE_URL}"
    )


def build_tweet2_trimmed(roll_call: WeeklyRollCall) -> str:
    """Fallback if the full version is rejected for length."""
    return (
        f"Submit a weekly health attestation: {FORMS_URL}\n\n"
        f"Top ten weekly: {_roll_call_url(roll_call)}\n\n"
        f"Discord: {DISCORD_URL} | {WEBSITE_URL}"
    )


def _find_ode_path(week_end: date) -> Path:
    base = Path(settings.BASE_DIR) / "logs" / week_end.isoformat()
    for fname in ("ode.md", "ode.txt"):
        p = base / fname
        if p.exists():
            return p
    raise CommandError(f"No ode found in {base}/")


class Command(BaseCommand):
    help = "Post weekly rankings + winner stanza image to 𝕏 as a 2-tweet thread"

    def add_arguments(self, parser):
        parser.add_argument(
            "--week-end",
            type=str,
            required=True,
            help="Week-end Sunday (YYYY-MM-DD)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print tweet text without posting",
        )
        parser.add_argument(
            "--no-sleep",
            action="store_true",
            help="Skip the 60s sleep between tweets (debug only)",
        )

    def handle(self, *args, **options):
        from datetime import timedelta as _td

        week_end = date.fromisoformat(options["week_end"])
        if week_end.weekday() != 6:
            raise CommandError(f"{week_end} is not a Sunday")
        week_start = week_end - _td(days=6)

        roll_call = WeeklyRollCall.objects.filter(week_start_date=week_start).first()
        if not roll_call:
            raise CommandError(f"No WeeklyRollCall for week starting {week_start}")

        ranked = _load_ranked(roll_call)
        tweet1_text = build_tweet1(roll_call, ranked)
        tweet2_text = build_tweet2_full(roll_call)
        tweet2_trim = build_tweet2_trimmed(roll_call)

        ode_path = _find_ode_path(week_end)
        winner_png = ode_path.parent / "winner.png"
        render_winner_image(ode_path.read_text(), winner_png)
        self.stdout.write(f"Rendered winner image: {winner_png}")

        self.stdout.write(self.style.NOTICE(f"=== TWEET 1 ({len(tweet1_text)} chars) ==="))
        self.stdout.write(tweet1_text)
        self.stdout.write(self.style.NOTICE(f"=== TWEET 2 full ({len(tweet2_text)} chars) ==="))
        self.stdout.write(tweet2_text)
        self.stdout.write(self.style.NOTICE(f"=== TWEET 2 trimmed fallback ({len(tweet2_trim)} chars) ==="))
        self.stdout.write(tweet2_trim)

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — not posting"))
            return

        for k in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
            if not getattr(settings, k, None):
                raise CommandError(f"{k} not configured")

        import tweepy

        auth = tweepy.OAuth1UserHandler(
            settings.X_CONSUMER_KEY,
            settings.X_CONSUMER_SECRET,
            settings.X_ACCESS_TOKEN,
            settings.X_ACCESS_TOKEN_SECRET,
        )
        api_v1 = tweepy.API(auth)
        client = tweepy.Client(
            consumer_key=settings.X_CONSUMER_KEY,
            consumer_secret=settings.X_CONSUMER_SECRET,
            access_token=settings.X_ACCESS_TOKEN,
            access_token_secret=settings.X_ACCESS_TOKEN_SECRET,
        )

        media = api_v1.media_upload(filename=str(winner_png))
        self.stdout.write(f"media uploaded: id={media.media_id}")

        r1 = client.create_tweet(text=tweet1_text, media_ids=[media.media_id])
        tweet1_id = r1.data["id"]
        url1 = f"https://x.com/StrongAsAn0x/status/{tweet1_id}"
        self.stdout.write(self.style.SUCCESS(f"✅ Tweet 1: {url1}"))

        sleep_s = 0 if options["no_sleep"] else SLEEP_BETWEEN_TWEETS_SECONDS
        if sleep_s:
            self.stdout.write(f"sleeping {sleep_s}s before reply...")
            time.sleep(sleep_s)

        for label, text in [("full", tweet2_text), ("trimmed", tweet2_trim)]:
            try:
                r2 = client.create_tweet(text=text, in_reply_to_tweet_id=tweet1_id)
                url2 = f"https://x.com/StrongAsAn0x/status/{r2.data['id']}"
                self.stdout.write(self.style.SUCCESS(f"✅ Tweet 2 ({label}): {url2}"))
                return
            except tweepy.TweepyException as e:
                self.stdout.write(self.style.WARNING(f"❌ Tweet 2 {label} failed: {e}"))
                continue

        raise CommandError("Both tweet 2 variants failed; tweet 1 is live but reply did not post.")
