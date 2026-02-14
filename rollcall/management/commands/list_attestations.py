"""
List recent attestations from Discord and Telegram
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from datetime import datetime, timedelta, timezone
from rollcall.models import Attestation


class Command(BaseCommand):
    help = 'List recent attestations from Discord and Telegram'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=3,
            help='Number of days to look back (default: 3)',
        )
        parser.add_argument(
            '--source',
            type=str,
            choices=['discord', 'telegram', 'all'],
            default='all',
            help='Filter by source (default: all)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=50,
            help='Maximum number of attestations to show (default: 50)',
        )

    def handle(self, *args, **options):
        days = options['days']
        source_filter = options['source']
        limit = options['limit']
        
        # Calculate cutoff time
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)
        
        # Build query
        query = Q(posted_at__gte=cutoff_time, parent_attestation__isnull=True)  # Only main attestations
        
        if source_filter != 'all':
            query &= Q(source=source_filter)
        
        # Get attestations
        attestations = Attestation.objects.filter(query).select_related(
            'discord_user', 'telegram_user', 'weekly_roll_call'
        ).order_by('-posted_at')[:limit]
        
        if not attestations:
            self.stdout.write(self.style.WARNING(f"No attestations found in the last {days} days"))
            return
        
        # Print header
        self.stdout.write("\n" + "="*80)
        self.stdout.write(self.style.SUCCESS(
            f"Recent Attestations (last {days} days, showing {len(attestations)} of {Attestation.objects.filter(query).count()} total)"
        ))
        self.stdout.write("="*80 + "\n")
        
        # Print attestations
        for att in attestations:
            # Get user info
            if att.source == 'discord':
                user_name = att.discord_user.discord_display_name if att.discord_user else "Unknown"
                linked_name = att.discord_user.linked_name if att.discord_user else "Not linked"
                user_id = att.discord_user.discord_user_id if att.discord_user else "N/A"
            else:
                user_name = f"{att.telegram_user.telegram_first_name} {att.telegram_user.telegram_last_name}".strip() if att.telegram_user else "Unknown"
                if not user_name:
                    user_name = att.telegram_user.telegram_username if att.telegram_user else "Unknown"
                linked_name = att.telegram_user.linked_name if att.telegram_user else "Not linked"
                user_id = att.telegram_user.telegram_user_id if att.telegram_user else "N/A"
            
            # Format timestamp
            timestamp_str = att.posted_at.strftime('%Y-%m-%d %H:%M:%S UTC')
            
            # Source icon
            source_icon = "💬" if att.source == 'discord' else "📱"
            
            # Preview content (first 150 chars)
            content_preview = att.raw_text[:150] + "..." if len(att.raw_text) > 150 else att.raw_text
            
            # Check for parts
            parts_count = att.parts.count() if hasattr(att, 'parts') else 0
            parts_info = f" ({parts_count} parts)" if parts_count > 0 else ""

            # Check hidden status
            hidden_info = " [HIDDEN]" if att.is_hidden else ""

            # Print attestation
            self.stdout.write(f"{source_icon} [{timestamp_str}]{hidden_info}")
            self.stdout.write(f"   User: {user_name} (Linked: {linked_name})")
            self.stdout.write(f"   Week: {att.weekly_roll_call.week_start_date} to {att.weekly_roll_call.week_end_date} (Published: {att.weekly_roll_call.is_published})")
            self.stdout.write(f"   Message ID: {att.message_id}{parts_info}")
            if att.has_attachments:
                self.stdout.write(f"   📎 {att.attachment_count} attachment(s)")
            self.stdout.write("   Content preview:")
            self.stdout.write(f"   {self.style.WARNING('   ' + content_preview.replace(chr(10), chr(10) + '   '))}")
            self.stdout.write("")
        
        # Print summary
        self.stdout.write("="*80)
        total_count = Attestation.objects.filter(query).count()
        discord_count = Attestation.objects.filter(query & Q(source='discord')).count()
        telegram_count = Attestation.objects.filter(query & Q(source='telegram')).count()
        hidden_count = Attestation.objects.filter(query & Q(is_hidden=True)).count()

        self.stdout.write("Summary:")
        self.stdout.write(f"  Total attestations: {total_count}")
        self.stdout.write(f"  💬 Discord: {discord_count}")
        self.stdout.write(f"  📱 Telegram: {telegram_count}")
        if hidden_count > 0:
            self.stdout.write(f"  🚫 Hidden: {hidden_count}")
        self.stdout.write("="*80 + "\n")


