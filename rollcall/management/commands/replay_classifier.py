"""Replay recent MessageLog rows through the Sonnet classifier.

No DB writes. Reports disagreements with the existing heuristic detector and
emits a CSV/JSONL of verdicts for offline tuning.

Usage:
    python manage.py replay_classifier --last 200
    python manage.py replay_classifier --last 1000 --output disagreements.csv
    python manage.py replay_classifier --since 2026-05-01 --only-disagreements
"""
import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand

from rollcall.models import MessageLog
from rollcall.telegram_bot.conversation.classifier import (
    ClassifierInput,
    classify_message,
    pacific_now,
)
from rollcall.telegram_bot.utils.attestation_detector import (
    is_likely_attestation,
    is_weekend_message,
)


class Command(BaseCommand):
    help = "Replay recent MessageLog rows through the Sonnet classifier (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--last", type=int, default=100, help="How many recent rows to replay")
        parser.add_argument("--since", type=str, help="ISO date — replay only rows posted_at >= this")
        parser.add_argument("--source", type=str, default="telegram", help="Source filter (telegram/discord)")
        parser.add_argument("--output", type=str, help="Path to write CSV (default: stdout summary only)")
        parser.add_argument(
            "--only-disagreements",
            action="store_true",
            help="Only print/output rows where classifier and heuristic disagree",
        )
        parser.add_argument(
            "--exclude-bot-replies",
            action="store_true",
            default=True,
            help="Skip is_bot_reply=True rows (default on)",
        )

    def handle(self, *args, **opts):
        qs = MessageLog.objects.filter(source=opts["source"]).order_by("-posted_at")
        if opts.get("since"):
            since = datetime.fromisoformat(opts["since"]).replace(tzinfo=timezone.utc)
            qs = qs.filter(posted_at__gte=since)
        if opts.get("exclude_bot_replies"):
            qs = qs.exclude(is_bot_reply=True)
        qs = qs[: opts["last"]]

        rows = list(qs)
        self.stdout.write(self.style.NOTICE(f"Replaying {len(rows)} messages..."))

        results = asyncio.run(self._classify_all(rows))

        # Summary
        agree = 0
        disagree = 0
        classifier_yes = 0
        heuristic_yes = 0
        for r in results:
            if r["heuristic_is_attestation"] == r["classifier_is_attestation"]:
                agree += 1
            else:
                disagree += 1
            if r["classifier_is_attestation"]:
                classifier_yes += 1
            if r["heuristic_is_attestation"]:
                heuristic_yes += 1

        self.stdout.write(self.style.SUCCESS(
            f"Agreement: {agree}/{len(results)}  Disagreements: {disagree}  "
            f"(classifier-yes: {classifier_yes}  heuristic-yes: {heuristic_yes})"
        ))

        rows_to_emit = [r for r in results if r["heuristic_is_attestation"] != r["classifier_is_attestation"]] \
            if opts.get("only_disagreements") else results

        if opts.get("output"):
            path = Path(opts["output"])
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=rows_to_emit[0].keys() if rows_to_emit else [])
                writer.writeheader()
                for r in rows_to_emit:
                    writer.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
            self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows_to_emit)} rows to {path}"))
        else:
            # Just print disagreements summary to stdout
            for r in rows_to_emit[:20]:
                self.stdout.write(
                    f"\n{'='*60}\n"
                    f"msg_id={r['message_id']} chat={r['chat_id']} from={r['sender']}\n"
                    f"heuristic={r['heuristic_is_attestation']}  "
                    f"classifier={r['classifier_is_attestation']} (conf={r['attestation_confidence']:.2f}) "
                    f"intent={r['intent']}\n"
                    f"reply_reason: {r['reply_reason']}\n"
                    f"text: {r['text'][:200]}"
                )

    async def _classify_all(self, rows):
        results = []
        for i, row in enumerate(rows):
            text = row.content or ""
            if len(text.strip()) < 50:
                continue
            heuristic_yes = is_likely_attestation(text, row.posted_at)
            features = ClassifierInput(
                text=text,
                chat_type="supergroup",  # historical, we don't have chat_type stored
                is_mention_or_reply=False,
                sender_display=(row.telegram_user.telegram_first_name if row.telegram_user else "?"),
                sender_linked_warrior=(row.telegram_user.linked_name if row.telegram_user and row.telegram_user.linked_name else None),
                pacific_now=pacific_now(),
                is_weekend_window=is_weekend_message(row.posted_at),
                recent_history=[],
                has_image=row.has_attachments,
            )
            verdict = await classify_message(features)
            results.append({
                "message_id": row.telegram_message_id,
                "chat_id": row.telegram_chat_id,
                "posted_at": row.posted_at.isoformat(),
                "sender": features.sender_display,
                "text": text,
                "heuristic_is_attestation": heuristic_yes,
                "classifier_is_attestation": verdict.is_attestation,
                "attestation_confidence": verdict.attestation_confidence,
                "should_reply": verdict.should_reply,
                "reply_reason": verdict.reply_reason,
                "intent": verdict.intent,
                "target_warrior": verdict.target_warrior,
                "model": verdict.model,
                "latency_ms": verdict.latency_ms,
                "error": verdict.error,
            })
            if (i + 1) % 25 == 0:
                self.stdout.write(f"  ... {i+1}/{len(rows)}")
        return results
