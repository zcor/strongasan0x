from django.core.management.base import BaseCommand, CommandError
from rollcall.models import DiscordUserMapping, RollCallRanking

class Command(BaseCommand):
    help = 'Link a Discord user to a contest participant by name or Twitter handle'

    def add_arguments(self, parser):
        parser.add_argument(
            '--discord-user-id',
            type=int,
            required=True,
            help='Discord user ID to link'
        )
        parser.add_argument(
            '--discord-username',
            type=str,
            required=True,
            help='Discord username'
        )
        parser.add_argument(
            '--discord-display-name',
            type=str,
            default='',
            help='Discord display name (optional)'
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
        discord_user_id = options['discord_user_id']
        discord_username = options['discord_username']
        discord_display_name = options['discord_display_name'] or discord_username
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
        mapping, created = DiscordUserMapping.objects.get_or_create(
            discord_user_id=discord_user_id,
            defaults={
                'discord_username': discord_username,
                'discord_display_name': discord_display_name,
                'is_active': True
            }
        )
        
        # Update mapping
        mapping.discord_username = discord_username
        mapping.discord_display_name = discord_display_name
        mapping.linked_name = ranking.name
        if ranking.twitter_handle:
            mapping.linked_twitter_handle = ranking.twitter_handle
        mapping.is_active = True
        mapping.save()
        
        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f'{action} link: {discord_username} → {ranking.name} '
                f'(Rank {ranking.rank} in week of {ranking.weekly_roll_call.week_start_date})'
            )
        )


