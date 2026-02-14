from django.core.management.base import BaseCommand, CommandError
from rollcall.models import TelegramUserMapping, RollCallRanking


class Command(BaseCommand):
    help = 'Link a Telegram user to a contest participant by name or Twitter handle'

    def add_arguments(self, parser):
        parser.add_argument(
            '--telegram-user-id',
            type=int,
            required=True,
            help='Telegram user ID to link'
        )
        parser.add_argument(
            '--telegram-username',
            type=str,
            default='',
            help='Telegram username'
        )
        parser.add_argument(
            '--telegram-first-name',
            type=str,
            required=True,
            help='Telegram first name'
        )
        parser.add_argument(
            '--telegram-last-name',
            type=str,
            default='',
            help='Telegram last name (optional)'
        )
        parser.add_argument(
            '--name',
            type=str,
            help='Contest participant name to link to'
        )
        parser.add_argument(
            '--twitter-handle',
            type=str,
            help='Twitter handle to link to (without @)'
        )

    def handle(self, *args, **options):
        telegram_user_id = options['telegram_user_id']
        telegram_username = options['telegram_username']
        telegram_first_name = options['telegram_first_name']
        telegram_last_name = options.get('telegram_last_name', '')
        name = options.get('name')
        twitter_handle = options.get('twitter_handle')
        
        if not name and not twitter_handle:
            raise CommandError('Either --name or --twitter-handle must be provided')
        
        # Find ranking
        ranking = None
        if name:
            ranking = RollCallRanking.objects.filter(name__iexact=name).first()
        elif twitter_handle:
            ranking = RollCallRanking.objects.filter(twitter_handle__iexact=twitter_handle).first()
        
        if not ranking:
            raise CommandError(
                f'No contest participant found matching '
                f'{"name: " + name if name else "twitter_handle: " + twitter_handle}'
            )
        
        # Get or create mapping
        mapping, created = TelegramUserMapping.objects.get_or_create(
            telegram_user_id=telegram_user_id,
            defaults={
                'telegram_username': telegram_username,
                'telegram_first_name': telegram_first_name,
                'telegram_last_name': telegram_last_name,
                'is_active': True
            }
        )
        
        # Update mapping
        mapping.telegram_username = telegram_username
        mapping.telegram_first_name = telegram_first_name
        mapping.telegram_last_name = telegram_last_name
        mapping.linked_name = ranking.name
        if ranking.twitter_handle:
            mapping.linked_twitter_handle = ranking.twitter_handle
        mapping.is_active = True
        mapping.save()
        
        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f'{action} link: {telegram_first_name} → {ranking.name} '
                f'(Rank {ranking.rank} in week of {ranking.weekly_roll_call.week_start_date})'
            )
        )



