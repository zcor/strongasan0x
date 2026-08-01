from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db.models import Max
from datetime import date, datetime, timedelta
from rollcall.models import (
    Attestation, WeeklyRollCall, DiscordUserMapping, TelegramUserMapping, RollCallRanking
)


class Command(BaseCommand):
    help = 'Import an attestation manually (for attestations posted outside Discord/Telegram bots)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--name',
            type=str,
            required=True,
            help='Contest participant name (e.g., "RektDiomedes")'
        )
        parser.add_argument(
            '--text',
            type=str,
            help='Attestation text (alternative to --text-file)'
        )
        parser.add_argument(
            '--text-file',
            type=str,
            help='Path to file containing attestation text'
        )
        parser.add_argument(
            '--week-end',
            type=str,
            help='Week end date (Sunday) in YYYY-MM-DD format. Defaults to most recent Sunday.'
        )
        parser.add_argument(
            '--posted-at',
            type=str,
            help='When the attestation was posted (YYYY-MM-DD HH:MM:SS). Defaults to now.'
        )
        parser.add_argument(
            '--source',
            type=str,
            choices=['discord', 'telegram'],
            default='discord',
            help='Source platform (default: discord)'
        )

    def handle(self, *args, **options):
        # Get attestation text
        text = None
        if options['text_file']:
            try:
                with open(options['text_file'], 'r', encoding='utf-8') as f:
                    text = f.read().strip()
            except FileNotFoundError:
                raise CommandError(f'Text file not found: {options["text_file"]}')
            except Exception as e:
                raise CommandError(f'Error reading text file: {e}')
        elif options['text']:
            text = options['text'].strip()
        else:
            raise CommandError('Either --text or --text-file must be provided.')

        if not text:
            raise CommandError('Attestation text cannot be empty.')

        # Find user by name
        name = options['name']
        ranking = RollCallRanking.objects.filter(name__iexact=name).first()
        
        if not ranking:
            raise CommandError(
                f'No contest participant found with name "{name}". '
                'Available names: ' + ', '.join(
                    RollCallRanking.objects.values_list('name', flat=True).distinct()[:10]
                )
            )

        # Find or create user mapping
        source = options['source']
        if source == 'discord':
            # Try to find existing Discord user mapping
            user_mapping = DiscordUserMapping.objects.filter(
                linked_name__iexact=name
            ).first()
            
            if not user_mapping:
                # Create a placeholder Discord user mapping for manual imports
                # Use a very large number (starting from 900000000000000000) to avoid conflicts
                max_id = DiscordUserMapping.objects.aggregate(
                    max_id=Max('discord_user_id')
                )['max_id'] or 0
                # Use a high base number for manual imports
                base_id = 900000000000000000
                if max_id >= base_id:
                    placeholder_id = max_id + 1
                else:
                    placeholder_id = base_id
                
                user_mapping = DiscordUserMapping.objects.create(
                    discord_user_id=placeholder_id,
                    discord_username=f"{name}_manual_import",
                    discord_display_name=name,
                    linked_name=ranking.name,
                    linked_twitter_handle=ranking.twitter_handle or '',
                    is_active=True
                )
                self.stdout.write(
                    self.style.WARNING(
                        f'Created placeholder Discord user mapping for {name} '
                        f'(ID: {placeholder_id})'
                    )
                )
        else:
            # Telegram
            user_mapping = TelegramUserMapping.objects.filter(
                linked_name__iexact=name
            ).first()
            
            if not user_mapping:
                # Create a placeholder Telegram user mapping
                # Use a very large number (starting from 900000000000000000) to avoid conflicts
                max_id = TelegramUserMapping.objects.aggregate(
                    max_id=Max('telegram_user_id')
                )['max_id'] or 0
                # Use a high base number for manual imports
                base_id = 900000000000000000
                if max_id >= base_id:
                    placeholder_id = max_id + 1
                else:
                    placeholder_id = base_id
                
                user_mapping = TelegramUserMapping.objects.create(
                    telegram_user_id=placeholder_id,
                    telegram_first_name=name,
                    telegram_username=f"{name.lower().replace(' ', '_')}_manual",
                    linked_name=ranking.name,
                    linked_twitter_handle=ranking.twitter_handle or '',
                    is_active=True
                )
                self.stdout.write(
                    self.style.WARNING(
                        f'Created placeholder Telegram user mapping for {name} '
                        f'(ID: {placeholder_id})'
                    )
                )

        # Determine week
        if options['week_end']:
            try:
                week_end = date.fromisoformat(options['week_end'])
                if week_end.weekday() != 6:  # Sunday is 6
                    raise CommandError(
                        f'Week end date must be a Sunday. '
                        f'{week_end.strftime("%A, %B %d, %Y")} is a {week_end.strftime("%A")}.'
                    )
                week_start = week_end - timedelta(days=6)
            except ValueError:
                raise CommandError(
                    f'Invalid date format: {options["week_end"]}. Use YYYY-MM-DD format.'
                )
        else:
            # Default to most recent Sunday
            today = date.today()
            days_since_sunday = (today.weekday() + 1) % 7
            if days_since_sunday == 0:
                week_end = today
            else:
                week_end = today - timedelta(days=days_since_sunday)
            week_start = week_end - timedelta(days=6)

        # Get or create weekly roll call
        roll_call = WeeklyRollCall.objects.filter(week_start_date=week_start).first()
        if not roll_call:
            # Create a minimal roll call if it doesn't exist
            roll_call = WeeklyRollCall.objects.create(
                week_start_date=week_start,
                week_end_date=week_end,
                substack_url='',  # Will need to be updated later
                full_text=f'Roll call for week of {week_start} (created automatically for attestation import)'
            )
            self.stdout.write(
                self.style.WARNING(
                    f'Created weekly roll call for week of {week_start} '
                    f'(update it with the on-site Roll Call URL before publishing)'
                )
            )

        # Parse posted_at
        if options['posted_at']:
            try:
                posted_at = datetime.fromisoformat(options['posted_at'])
                if posted_at.tzinfo is None:
                    posted_at = timezone.make_aware(posted_at)
            except ValueError:
                raise CommandError(
                    f'Invalid datetime format: {options["posted_at"]}. '
                    'Use YYYY-MM-DD HH:MM:SS format.'
                )
        else:
            posted_at = timezone.now()

        # Check if attestation already exists for this user and week
        if source == 'discord':
            existing = Attestation.objects.filter(
                weekly_roll_call=roll_call,
                discord_user=user_mapping,
                source='discord',
                parent_attestation__isnull=True
            ).first()
        else:
            existing = Attestation.objects.filter(
                weekly_roll_call=roll_call,
                telegram_user=user_mapping,
                source='telegram',
                parent_attestation__isnull=True
            ).first()

        if existing:
            raise CommandError(
                f'Attestation already exists for {name} for week of {week_start}. '
                f'Existing attestation ID: {existing.id}'
            )

        # Create attestation
        attestation_data = {
            'weekly_roll_call': roll_call,
            'source': source,
            'raw_text': text,
            'posted_at': posted_at,
            'has_attachments': False,
            'attachment_count': 0,
            'parent_attestation': None,
            'part_number': 1
        }

        if source == 'discord':
            attestation_data['discord_user'] = user_mapping
            # Use placeholder message ID for manual imports
            attestation_data['discord_message_id'] = None
            attestation_data['discord_channel_id'] = None
        else:
            attestation_data['telegram_user'] = user_mapping
            attestation_data['telegram_message_id'] = None
            attestation_data['telegram_chat_id'] = None

        attestation = Attestation.objects.create(**attestation_data)

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully imported attestation!\n'
                f'  User: {name}\n'
                f'  Week: {week_start} to {week_end}\n'
                f'  Source: {source}\n'
                f'  Posted at: {posted_at}\n'
                f'  Attestation ID: {attestation.id}\n'
                f'  Text length: {len(text)} characters'
            )
        )
