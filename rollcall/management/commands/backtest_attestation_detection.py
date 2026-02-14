"""
Backtest attestation detection logic against historical MessageLog data.
Compares current detection logic against stored Attestation records.
"""
from django.core.management.base import BaseCommand
from datetime import date, timedelta
from rollcall.models import MessageLog, Attestation
from rollcall.telegram_bot.utils.attestation_detector import (
    is_likely_attestation,
    is_weekend_message,
    has_attestation_structure,
    has_metrics,
    has_attestation_keywords,
    has_intent_markers,
    has_conversational_markers
)


class Command(BaseCommand):
    help = 'Backtest attestation detection logic against historical data'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=60,
            help='Number of days to look back (default: 60)'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed breakdown for each message'
        )

    def handle(self, *args, **options):
        days = options['days']
        verbose = options['verbose']

        cutoff_date = date.today() - timedelta(days=days)

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("ATTESTATION DETECTION BACKTEST")
        self.stdout.write(f"{'='*60}")
        self.stdout.write(f"Looking back {days} days (since {cutoff_date})")

        # Get all MessageLog entries from Telegram
        messages = MessageLog.objects.filter(
            source='telegram',
            posted_at__date__gte=cutoff_date
        ).select_related('telegram_user')

        # Get all actual attestations
        attestation_msg_ids = set(
            Attestation.objects.filter(
                source='telegram',
                posted_at__date__gte=cutoff_date
            ).values_list('telegram_message_id', flat=True)
        )

        self.stdout.write(f"\nMessages analyzed: {messages.count()}")
        self.stdout.write(f"Actual attestations in DB: {len(attestation_msg_ids)}")

        # Analyze each message
        true_positives = []
        false_positives = []
        false_negatives = []
        true_negatives = []

        for msg in messages:
            text = msg.content or ''
            if len(text.strip()) < 50:
                continue  # Skip very short messages

            is_actual_attestation = msg.telegram_message_id in attestation_msg_ids
            would_flag = is_likely_attestation(text, msg.posted_at)

            # Get details for verbose output
            details = {
                'msg_id': msg.telegram_message_id,
                'user': msg.telegram_user.linked_name if msg.telegram_user else 'Unknown',
                'date': msg.posted_at.strftime('%Y-%m-%d'),
                'text_preview': text[:80].replace('\n', ' '),
                'structure': has_attestation_structure(text),
                'metrics': has_metrics(text),
                'keywords': has_attestation_keywords(text),
                'intent': has_intent_markers(text),
                'conversational': has_conversational_markers(text),
                'weekend': is_weekend_message(msg.posted_at),
            }

            if is_actual_attestation and would_flag:
                true_positives.append(details)
            elif not is_actual_attestation and would_flag:
                false_positives.append(details)
            elif is_actual_attestation and not would_flag:
                false_negatives.append(details)
            else:
                true_negatives.append(details)

        # Print results
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("RESULTS")
        self.stdout.write(f"{'='*60}")

        total = len(true_positives) + len(false_positives) + len(false_negatives) + len(true_negatives)
        self.stdout.write(f"\nTotal messages analyzed: {total}")
        self.stdout.write(f"\nTrue Positives (correctly flagged): {len(true_positives)}")
        self.stdout.write(f"False Positives (incorrectly flagged): {len(false_positives)}")
        self.stdout.write(f"False Negatives (missed attestations): {len(false_negatives)}")
        self.stdout.write(f"True Negatives (correctly ignored): {len(true_negatives)}")

        if len(attestation_msg_ids) > 0:
            precision = len(true_positives) / (len(true_positives) + len(false_positives)) if (len(true_positives) + len(false_positives)) > 0 else 0
            recall = len(true_positives) / (len(true_positives) + len(false_negatives)) if (len(true_positives) + len(false_negatives)) > 0 else 0
            self.stdout.write(f"\nPrecision: {precision:.1%}")
            self.stdout.write(f"Recall: {recall:.1%}")

        # Show false positives
        if false_positives:
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write("FALSE POSITIVES (would incorrectly flag as attestation):")
            self.stdout.write(f"{'='*60}")
            for i, fp in enumerate(false_positives[:10], 1):
                self.stdout.write(f"\n{i}. [{fp['user']}] {fp['date']}")
                self.stdout.write(f"   \"{fp['text_preview']}...\"")
                self.stdout.write(f"   Structure:{fp['structure']} Metrics:{fp['metrics']} Keywords:{fp['keywords']} Intent:{fp['intent']} Conv:{fp['conversational']}")
            if len(false_positives) > 10:
                self.stdout.write(f"\n   ... and {len(false_positives) - 10} more")

        # Show false negatives
        if false_negatives:
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write("FALSE NEGATIVES (would miss these attestations):")
            self.stdout.write(f"{'='*60}")
            for i, fn in enumerate(false_negatives[:10], 1):
                self.stdout.write(f"\n{i}. [{fn['user']}] {fn['date']}")
                self.stdout.write(f"   \"{fn['text_preview']}...\"")
                self.stdout.write(f"   Structure:{fn['structure']} Metrics:{fn['metrics']} Keywords:{fn['keywords']} Intent:{fn['intent']} Conv:{fn['conversational']}")
            if len(false_negatives) > 10:
                self.stdout.write(f"\n   ... and {len(false_negatives) - 10} more")

        if verbose and true_positives:
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write("TRUE POSITIVES (correctly identified):")
            self.stdout.write(f"{'='*60}")
            for i, tp in enumerate(true_positives[:5], 1):
                self.stdout.write(f"\n{i}. [{tp['user']}] {tp['date']}")
                self.stdout.write(f"   \"{tp['text_preview']}...\"")
                self.stdout.write(f"   Structure:{tp['structure']} Metrics:{tp['metrics']} Keywords:{tp['keywords']} Intent:{tp['intent']}")

        self.stdout.write(f"\n{'='*60}")
