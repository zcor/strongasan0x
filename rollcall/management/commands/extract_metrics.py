"""
Extract structured metrics from attestation text using AI.

Usage:
    python manage.py extract_metrics                      # All unprocessed (DeepSeek default)
    python manage.py extract_metrics --provider anthropic  # Use Claude instead
    python manage.py extract_metrics --warrior CurveCap   # One warrior only
    python manage.py extract_metrics --week-start 2025-01-06  # One week only
    python manage.py extract_metrics --reextract           # Re-process existing records
    python manage.py extract_metrics --dry-run             # Preview without API calls
    python manage.py extract_metrics --limit 5             # Process at most 5
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from datetime import date

from rollcall.models import Attestation, ExtractedMetrics
from rollcall.services.metric_extraction import extract_and_save, get_full_text, METRIC_FIELDS, PROVIDERS


class Command(BaseCommand):
    help = 'Extract structured metrics from attestation text using AI (DeepSeek default)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--provider',
            type=str,
            choices=list(PROVIDERS.keys()),
            help='AI provider to use (default: deepseek)',
        )
        parser.add_argument(
            '--week-start',
            type=str,
            help='Process only attestations for this week (Monday YYYY-MM-DD)',
        )
        parser.add_argument(
            '--warrior',
            type=str,
            help='Process only attestations for this warrior (linked_name)',
        )
        parser.add_argument(
            '--reextract',
            action='store_true',
            help='Re-process attestations that already have ExtractedMetrics',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be extracted without calling API',
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Process at most N attestations',
        )

    def handle(self, *args, **options):
        # Base queryset: top-level, not hidden, published weeks only
        qs = Attestation.objects.filter(
            parent_attestation__isnull=True,
            is_hidden=False,
            weekly_roll_call__is_published=True,
        ).select_related(
            'weekly_roll_call', 'discord_user', 'telegram_user',
        ).order_by('weekly_roll_call__week_start_date', 'posted_at')

        # Filter by week
        if options['week_start']:
            try:
                week_start = date.fromisoformat(options['week_start'])
            except ValueError:
                raise CommandError(f"Invalid date: {options['week_start']}. Use YYYY-MM-DD.")
            qs = qs.filter(weekly_roll_call__week_start_date=week_start)

        # Filter by warrior name
        if options['warrior']:
            name = options['warrior']
            qs = qs.filter(
                Q(discord_user__linked_name__iexact=name) |
                Q(telegram_user__linked_name__iexact=name)
            )

        if not options['reextract']:
            # Only attestations without metrics, or with extraction errors
            qs = qs.filter(
                Q(metrics__isnull=True) | ~Q(metrics__extraction_error='')
            )

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No attestations to process."))
            return

        limit = options['limit']
        if limit:
            qs = qs[:limit]
            display_total = min(limit, total)
        else:
            display_total = total

        provider = options.get('provider')
        provider_label = provider or 'deepseek'
        self.stdout.write(f"Found {total} attestation(s) to process (showing {display_total}) using {provider_label}")

        if options['dry_run']:
            self._dry_run(qs, display_total)
            return

        success = 0
        errors = 0

        for i, attestation in enumerate(qs, 1):
            warrior_name = self._get_warrior_name(attestation)
            week = attestation.weekly_roll_call.week_start_date
            self.stdout.write(
                f"[{i}/{display_total}] Extracting {warrior_name} week of {week}... ",
                ending='',
            )

            try:
                metrics = extract_and_save(attestation, provider=provider)
                # Show a summary of non-null fields
                summary_parts = []
                for field in METRIC_FIELDS:
                    val = getattr(metrics, field, None)
                    if val is not None:
                        summary_parts.append(f"{field}={val}")
                summary = ", ".join(summary_parts) if summary_parts else "(no metrics found)"
                self.stdout.write(self.style.SUCCESS(f"done ({summary})"))
                success += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"FAILED: {e}"))
                errors += 1

        self.stdout.write("")
        self.stdout.write(f"Done: {success} succeeded, {errors} failed out of {display_total}")

    def _dry_run(self, qs, total):
        """Preview what would be extracted."""
        self.stdout.write(self.style.SUCCESS("\n--- DRY RUN (no API calls) ---\n"))

        for i, attestation in enumerate(qs, 1):
            warrior_name = self._get_warrior_name(attestation)
            week = attestation.weekly_roll_call.week_start_date
            full_text = get_full_text(attestation)
            has_metrics = hasattr(attestation, 'metrics') and ExtractedMetrics.objects.filter(attestation=attestation).exists()
            existing = ExtractedMetrics.objects.filter(attestation=attestation).first()
            status = "has metrics" if (existing and not existing.extraction_error) else (
                "has error" if (existing and existing.extraction_error) else "no metrics"
            )
            self.stdout.write(
                f"[{i}/{total}] {warrior_name} - week of {week} "
                f"({len(full_text)} chars, {status})"
            )

        self.stdout.write(self.style.SUCCESS(f"\nWould process {total} attestation(s)."))

    def _get_warrior_name(self, attestation):
        """Get display name for an attestation."""
        mapping = attestation.user_mapping
        if mapping and hasattr(mapping, 'linked_name') and mapping.linked_name:
            return mapping.linked_name
        if attestation.source == 'discord' and attestation.discord_user:
            return attestation.discord_user.discord_username
        if attestation.telegram_user:
            return attestation.telegram_user.telegram_first_name or attestation.telegram_user.telegram_username or "Unknown"
        return "Unknown"
