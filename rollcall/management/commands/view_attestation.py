"""
View full attestation text for users
"""
from django.core.management.base import BaseCommand
from rollcall.models import Attestation, WeeklyRollCall, DiscordUserMapping, TelegramUserMapping
from django.db.models import Q


class Command(BaseCommand):
    help = 'View full attestation text for a user or all users in the current week'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user',
            type=str,
            help='Username, linked name, or Telegram username to view (optional - if not provided, shows all)',
        )
        parser.add_argument(
            '--week',
            type=str,
            help='Week start date (YYYY-MM-DD) - defaults to most recent week',
        )
        parser.add_argument(
            '--source',
            type=str,
            choices=['discord', 'telegram', 'all'],
            default='all',
            help='Filter by source (default: all)',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Show all attestations for the current week',
        )

    def handle(self, *args, **options):
        user_filter = options.get('user')
        week_str = options.get('week')
        source_filter = options['source']
        show_all = options['all']
        
        # Get roll call
        if week_str:
            try:
                from datetime import datetime
                week_date = datetime.strptime(week_str, '%Y-%m-%d').date()
                roll_call = WeeklyRollCall.objects.filter(week_start_date=week_date).first()
                if not roll_call:
                    self.stdout.write(self.style.ERROR(f"No roll call found for week starting {week_str}"))
                    return
            except ValueError:
                self.stdout.write(self.style.ERROR("Invalid date format. Use YYYY-MM-DD"))
                return
        else:
            roll_call = WeeklyRollCall.objects.order_by('-week_start_date').first()
        
        if not roll_call:
            self.stdout.write(self.style.ERROR("No roll call data available"))
            return
        
        # Build query
        query = Q(weekly_roll_call=roll_call, parent_attestation__isnull=True)
        
        if source_filter != 'all':
            query &= Q(source=source_filter)
        
        if user_filter and not show_all:
            # Find user mapping(s) across both platforms; if both exist, include either
            discord_mapping = DiscordUserMapping.objects.filter(
                Q(discord_username__iexact=user_filter)
                | Q(linked_name__iexact=user_filter)
                | Q(linked_twitter_handle__iexact=user_filter)
            ).first()
            telegram_mapping = TelegramUserMapping.objects.filter(
                Q(telegram_username__iexact=user_filter)
                | Q(telegram_first_name__iexact=user_filter)
                | Q(linked_name__iexact=user_filter)
                | Q(linked_twitter_handle__iexact=user_filter)
            ).first()

            if discord_mapping and telegram_mapping:
                query &= Q(discord_user=discord_mapping) | Q(telegram_user=telegram_mapping)
            elif discord_mapping:
                query &= Q(discord_user=discord_mapping)
            elif telegram_mapping:
                query &= Q(telegram_user=telegram_mapping)
            else:
                self.stdout.write(self.style.ERROR(f"User '{user_filter}' not found"))
                return
        
        # Get attestations
        attestations = Attestation.objects.filter(query).select_related(
            'discord_user', 'telegram_user', 'weekly_roll_call'
        ).order_by('-posted_at')
        
        if not attestations:
            self.stdout.write(self.style.WARNING(
                f"No attestations found for week of {roll_call.week_start_date}"
            ))
            return
        
        # Print header
        self.stdout.write("\n" + "="*80)
        self.stdout.write(self.style.SUCCESS(
            f"Attestations for Week of {roll_call.week_start_date} to {roll_call.week_end_date}"
        ))
        self.stdout.write(f"Total: {attestations.count()}")
        self.stdout.write("="*80 + "\n")
        
        # Print each attestation
        for att in attestations:
            # Get user info
            if att.source == 'discord':
                user_name = att.discord_user.discord_display_name if att.discord_user else "Unknown"
                linked_name = att.discord_user.linked_name if att.discord_user else "Not linked"
            else:
                user_name = f"{att.telegram_user.telegram_first_name} {att.telegram_user.telegram_last_name}".strip()
                if not user_name:
                    user_name = att.telegram_user.telegram_username if att.telegram_user else "Unknown"
                linked_name = att.telegram_user.linked_name if att.telegram_user else "Not linked"
            
            # Get all parts
            parts = Attestation.objects.filter(
                Q(id=att.id) | Q(parent_attestation=att)
            ).order_by('part_number')
            
            # Print header for this attestation
            source_icon = "💬" if att.source == 'discord' else "📱"
            self.stdout.write(f"\n{source_icon} {'='*78}")
            self.stdout.write(self.style.SUCCESS(
                f"User: {user_name} (Linked: {linked_name})"
            ))
            self.stdout.write(f"Posted: {att.posted_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            self.stdout.write(f"Message ID: {att.message_id}")
            if att.has_attachments:
                self.stdout.write(f"📎 {att.attachment_count} attachment(s)")
            if parts.count() > 1:
                self.stdout.write(f"Parts: {parts.count()}")
            self.stdout.write(f"{'='*78}\n")
            
            # Print all parts
            for part in parts:
                if part.part_number > 1:
                    self.stdout.write(f"\n{self.style.WARNING('--- Part ' + str(part.part_number) + ' ---')}\n")
                
                # Print full text
                self.stdout.write(part.raw_text)
                
                if part.has_attachments:
                    self.stdout.write(f"\n📎 {part.attachment_count} attachment(s)")
                
                if part.part_number < parts.count():
                    self.stdout.write("\n")
            
            self.stdout.write(f"\n{source_icon} {'='*78}\n")
        
        # Summary
        self.stdout.write("\n" + "="*80)
        self.stdout.write(self.style.SUCCESS("Summary:"))
        self.stdout.write(f"  Week: {roll_call.week_start_date} to {roll_call.week_end_date}")
        self.stdout.write(f"  Total attestations: {attestations.count()}")
        discord_count = attestations.filter(source='discord').count()
        telegram_count = attestations.filter(source='telegram').count()
        self.stdout.write(f"  💬 Discord: {discord_count}")
        self.stdout.write(f"  📱 Telegram: {telegram_count}")
        self.stdout.write("="*80 + "\n")

