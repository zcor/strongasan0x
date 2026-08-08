"""Verify the live Roll Call and its social-publishing runtime without posting."""

from __future__ import annotations

from datetime import date
from importlib import import_module

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from rollcall.models import WeeklyRollCall
from rollcall.services.publish_runtime import telegram_roll_call_chat_id


REQUIRED_X_SETTINGS = (
    "X_CONSUMER_KEY",
    "X_CONSUMER_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)
ON_SITE_ROLL_CALL_PREFIX = "https://strongasan0x.com/roll-call/"


class Command(BaseCommand):
    help = "Check live Roll Call syndication prerequisites without posting to any channel"

    def add_arguments(self, parser):
        parser.add_argument(
            "--week-end",
            required=True,
            help="Week-ending Sunday in YYYY-MM-DD format",
        )

    def handle(self, *args, **options):
        try:
            week_end = date.fromisoformat(options["week_end"])
        except ValueError as exc:
            raise CommandError("--week-end must use YYYY-MM-DD") from exc
        if week_end.weekday() != 6:
            raise CommandError(f"{week_end} is not a Sunday")

        errors: list[str] = []
        try:
            roll_call = WeeklyRollCall.objects.get(week_end_date=week_end)
        except WeeklyRollCall.DoesNotExist as exc:
            raise CommandError(f"No Roll Call exists for week ending {week_end}") from exc

        if not roll_call.is_published:
            errors.append("Roll Call is not published")
        if len(roll_call.full_text or "") < 1000:
            errors.append("Roll Call text is missing or unexpectedly short")
        if not (roll_call.substack_url or "").startswith(ON_SITE_ROLL_CALL_PREFIX):
            errors.append("Roll Call URL is not the canonical on-site URL")
        elif roll_call.is_published:
            try:
                response = requests.get(roll_call.substack_url, timeout=15)
                if response.status_code != 200:
                    errors.append(f"Public Roll Call URL returned HTTP {response.status_code}")
            except requests.RequestException:
                errors.append("Public Roll Call URL could not be reached")

        for package in ("PIL", "tweepy"):
            try:
                import_module(package)
            except ImportError:
                errors.append(f"Missing publishing dependency: {package}")

        missing_x = [name for name in REQUIRED_X_SETTINGS if not getattr(settings, name, "")]
        if missing_x:
            errors.append("Missing X credential setting(s): " + ", ".join(missing_x))

        raw_chat_id = str(getattr(settings, "TELEGRAM_ATTESTATION_CHANNEL_ID", "") or "").strip()
        if not raw_chat_id:
            errors.append("TELEGRAM_ATTESTATION_CHANNEL_ID is not configured")
        else:
            try:
                int(raw_chat_id)
            except ValueError:
                errors.append("TELEGRAM_ATTESTATION_CHANNEL_ID is not numeric")

        token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
        expected_bot = (getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "StrongAsAn0xBot").lstrip("@")
        if not token:
            errors.append("TELEGRAM_BOT_TOKEN is not configured")
        else:
            api_base = f"https://api.telegram.org/bot{token}"
            try:
                identity = requests.get(f"{api_base}/getMe", timeout=15).json()
                actual_bot = identity.get("result", {}).get("username", "") if identity.get("ok") else ""
                if actual_bot.lower() != expected_bot.lower():
                    errors.append(f"Telegram bot is @{actual_bot or 'unknown'}, expected @{expected_bot}")

                chat = requests.get(
                    f"{api_base}/getChat",
                    params={"chat_id": telegram_roll_call_chat_id()},
                    timeout=15,
                ).json()
                if not chat.get("ok"):
                    errors.append("Telegram bot cannot access the configured Roll Call chat")
                elif chat.get("result", {}).get("type") not in {"group", "supergroup"}:
                    errors.append("Configured Telegram chat is not a group")
            except requests.RequestException:
                errors.append("Telegram API could not be reached")

        if errors:
            for error in errors:
                self.stderr.write(self.style.ERROR(f"✗ {error}"))
            raise CommandError("Roll Call syndication preflight failed; no social posts were sent")

        self.stdout.write(self.style.SUCCESS("✓ Roll Call is live at the canonical on-site URL"))
        self.stdout.write(self.style.SUCCESS("✓ Pillow and tweepy are installed"))
        self.stdout.write(self.style.SUCCESS("✓ X credentials are configured"))
        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Telegram @{expected_bot} can access chat {telegram_roll_call_chat_id()}"
            )
        )
