from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import models
import asyncio
import logging
import discord
from rollcall.models import DiscordUserMapping, TelegramUserMapping, RollCallRanking
from collections import defaultdict

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Show comprehensive summary of all user mappings and their status'

    def add_arguments(self, parser):
        parser.add_argument(
            '--format',
            choices=['table', 'json', 'csv'],
            default='table',
            help='Output format (default: table)',
        )

    def handle(self, *args, **options):
        if not settings.DISCORD_BOT_TOKEN:
            self.stdout.write(
                self.style.ERROR('DISCORD_BOT_TOKEN not set in environment variables')
            )
            return
        
        if not settings.DISCORD_GUILD_ID:
            self.stdout.write(
                self.style.ERROR('DISCORD_GUILD_ID not set in environment variables')
            )
            return

        async def get_discord_roles():
            """Get Discord role information for users"""
            intents = discord.Intents.default()
            intents.guilds = True
            intents.members = True
            
            bot = discord.Client(intents=intents)
            ready_event = asyncio.Event()
            
            @bot.event
            async def on_ready():
                ready_event.set()
            
            try:
                bot_task = asyncio.create_task(bot.start(settings.DISCORD_BOT_TOKEN))
                
                try:
                    await asyncio.wait_for(ready_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    bot_task.cancel()
                    return {}
                
                guild_id = int(settings.DISCORD_GUILD_ID)
                guild = bot.get_guild(guild_id)
                
                if not guild:
                    try:
                        guild = await bot.fetch_guild(guild_id)
                        await guild.chunk()
                    except:
                        return {}
                
                # Get Top 10 role (try both names)
                top_10_role_name = getattr(settings, 'DISCORD_TOP_10_ROLE_NAME', 'Top Ten')
                top_10_role = discord.utils.get(guild.roles, name=top_10_role_name)
                if not top_10_role and top_10_role_name == 'Top Ten':
                    top_10_role = discord.utils.get(guild.roles, name='Top 10')
                
                # Build mapping of user IDs to role status
                role_status = {}
                if top_10_role:
                    for member in guild.members:
                        if not member.bot:
                            role_status[member.id] = {
                                'has_top_10_role': top_10_role in member.roles,
                                'discord_display_name': member.display_name,
                                'discord_username': member.name,
                            }
                
                return role_status
            finally:
                try:
                    await bot.close()
                except:
                    pass
                if 'bot_task' in locals() and not bot_task.done():
                    bot_task.cancel()
                    try:
                        await bot_task
                    except (asyncio.CancelledError, Exception):
                        pass
        
        # Get all mappings
        def get_all_mappings():
            discord_mappings = list(DiscordUserMapping.objects.filter(is_active=True).order_by('discord_username'))
            telegram_mappings = list(TelegramUserMapping.objects.filter(is_active=True).order_by('telegram_first_name', 'telegram_username'))
            
            # Get all unique linked names to find rankings
            all_linked_names = set()
            all_twitter_handles = set()
            
            for dm in discord_mappings:
                if dm.linked_name:
                    all_linked_names.add(dm.linked_name)
                if dm.linked_twitter_handle:
                    all_twitter_handles.add(dm.linked_twitter_handle.lower())
            
            for tm in telegram_mappings:
                if tm.linked_name:
                    all_linked_names.add(tm.linked_name)
                if tm.linked_twitter_handle:
                    all_twitter_handles.add(tm.linked_twitter_handle.lower())
            
            # Get rankings for these names
            rankings = {}
            if all_linked_names or all_twitter_handles:
                ranking_objs = RollCallRanking.objects.filter(
                    models.Q(name__in=all_linked_names) | models.Q(twitter_handle__in=all_twitter_handles)
                ).select_related('weekly_roll_call').order_by('-weekly_roll_call__week_start_date', 'rank')
                
                for ranking in ranking_objs:
                    key = ranking.name.lower()
                    if key not in rankings or ranking.weekly_roll_call.week_start_date > rankings[key]['week_start_date']:
                        rankings[key] = {
                            'name': ranking.name,
                            'twitter_handle': ranking.twitter_handle,
                            'rank': ranking.rank,
                            'week_start_date': ranking.weekly_roll_call.week_start_date,
                        }
            
            return discord_mappings, telegram_mappings, rankings
        
        discord_mappings, telegram_mappings, rankings = get_all_mappings()
        
        # Get Discord role status
        self.stdout.write("Fetching Discord role information...")
        discord_roles = asyncio.run(get_discord_roles())
        
        # Combine mappings by linked name or Twitter handle
        combined_users = defaultdict(lambda: {
            'discord': None,
            'telegram': None,
            'ranking': None,
            'discord_role_status': None,
        })
        
        # Add Discord mappings
        for dm in discord_mappings:
            key = dm.linked_name.lower() if dm.linked_name else f"discord_{dm.discord_user_id}"
            combined_users[key]['discord'] = dm
            if dm.discord_user_id in discord_roles:
                combined_users[key]['discord_role_status'] = discord_roles[dm.discord_user_id]
        
        # Add Telegram mappings (merge with Discord if same linked_name)
        for tm in telegram_mappings:
            key = tm.linked_name.lower() if tm.linked_name else f"telegram_{tm.telegram_user_id}"
            combined_users[key]['telegram'] = tm
        
        # Add ranking information
        for ranking_key, ranking_data in rankings.items():
            if ranking_key in combined_users:
                combined_users[ranking_key]['ranking'] = ranking_data
        
        # Output based on format
        output_format = options.get('format', 'table')
        
        if output_format == 'table':
            # Table format
            headers = [
                'DB ID',
                'Discord ID',
                'Discord Username',
                'Discord Display',
                'Linked Name',
                'Twitter',
                'Latest Rank',
                'Week',
                'Top 10',
                'Django User'
            ]
            
            # Calculate column widths
            col_widths = [len(h) for h in headers]
            rows = []
            
            for key, user_data in sorted(combined_users.items()):
                row = []
                
                # DB ID (Discord or Telegram ID)
                if user_data['discord']:
                    db_id = str(user_data['discord'].discord_user_id)
                elif user_data['telegram']:
                    db_id = str(user_data['telegram'].telegram_user_id)
                else:
                    db_id = '-'
                row.append(db_id)
                
                # Discord ID
                row.append(str(user_data['discord'].discord_user_id) if user_data['discord'] else '-')
                
                # Discord Username (use current from Discord API if available, otherwise DB)
                if user_data['discord']:
                    if user_data.get('discord_role_status'):
                        # Use current Discord username from API
                        discord_username = user_data['discord_role_status']['discord_username']
                    else:
                        # Fall back to database value
                        discord_username = user_data['discord'].discord_username
                    row.append('@' + discord_username)
                else:
                    row.append('-')
                
                # Discord Display Name (use current from Discord API if available, otherwise DB)
                if user_data['discord']:
                    if user_data.get('discord_role_status'):
                        # Use current Discord display name from API
                        discord_display = user_data['discord_role_status']['discord_display_name']
                    else:
                        # Fall back to database value
                        discord_display = user_data['discord'].discord_display_name
                    row.append(discord_display)
                else:
                    row.append('-')
                
                # Linked Name
                linked_name = (user_data['discord'].linked_name if user_data['discord'] and user_data['discord'].linked_name else '') or \
                             (user_data['telegram'].linked_name if user_data['telegram'] and user_data['telegram'].linked_name else '') or \
                             (user_data['ranking']['name'] if user_data['ranking'] else '') or '-'
                row.append(linked_name)
                
                # Twitter
                twitter = (user_data['discord'].linked_twitter_handle if user_data['discord'] and user_data['discord'].linked_twitter_handle else '') or \
                         (user_data['telegram'].linked_twitter_handle if user_data['telegram'] and user_data['telegram'].linked_twitter_handle else '') or \
                         (user_data['ranking']['twitter_handle'] if user_data['ranking'] and user_data['ranking']['twitter_handle'] else '') or '-'
                row.append('@' + twitter if twitter != '-' else '-')
                
                # Latest Rank
                row.append(str(user_data['ranking']['rank']) if user_data['ranking'] else '-')
                
                # Week
                row.append(str(user_data['ranking']['week_start_date']) if user_data['ranking'] else '-')
                
                # Top 10 Role
                if user_data.get('discord_role_status'):
                    row.append('✅ YES' if user_data['discord_role_status']['has_top_10_role'] else '❌ NO')
                elif user_data['discord']:
                    row.append('⚠️  Not Found')
                else:
                    row.append('-')
                
                # Django User
                django_user = (user_data['discord'].user.username if user_data['discord'] and user_data['discord'].user else '') or \
                             (user_data['telegram'].user.username if user_data['telegram'] and user_data['telegram'].user else '') or '-'
                row.append(django_user)
                
                rows.append(row)
                
                # Update column widths
                for i, val in enumerate(row):
                    col_widths[i] = max(col_widths[i], len(str(val)))
            
            # Print table
            self.stdout.write("\n" + "=" * (sum(col_widths) + len(headers) * 3 + 1))
            self.stdout.write("USER MAPPINGS SUMMARY")
            self.stdout.write("=" * (sum(col_widths) + len(headers) * 3 + 1))
            
            # Header row
            header_row = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
            self.stdout.write(header_row)
            self.stdout.write("-" * (sum(col_widths) + len(headers) * 3 + 1))
            
            # Data rows
            for row in rows:
                data_row = " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row))
                self.stdout.write(data_row)
            
            self.stdout.write("=" * (sum(col_widths) + len(headers) * 3 + 1))
            self.stdout.write("\nSummary:")
            self.stdout.write(f"  Total Users: {len(combined_users)}")
            self.stdout.write(f"  With Discord: {sum(1 for u in combined_users.values() if u['discord'])}")
            self.stdout.write(f"  With Rankings: {sum(1 for u in combined_users.values() if u['ranking'])}")
            self.stdout.write(f"  With Top 10 Role: {sum(1 for u in combined_users.values() if u.get('discord_role_status') and u['discord_role_status'].get('has_top_10_role', False))}")
        
        elif output_format == 'json':
            import json
            output_data = []
            for key, user_data in sorted(combined_users.items()):
                record = {
                    'key': key,
                    'discord': None,
                    'telegram': None,
                    'ranking': None,
                    'has_top_10_role': False,
                }
                
                if user_data['discord']:
                    dm = user_data['discord']
                    # Use current Discord username/display from API if available
                    if user_data.get('discord_role_status'):
                        discord_username = user_data['discord_role_status']['discord_username']
                        discord_display = user_data['discord_role_status']['discord_display_name']
                    else:
                        discord_username = dm.discord_username
                        discord_display = dm.discord_display_name
                    
                    discord_data = {
                        'user_id': dm.discord_user_id,
                        'username': discord_username,
                        'display_name': discord_display,
                        'linked_name': dm.linked_name,
                        'linked_twitter_handle': dm.linked_twitter_handle,
                        'django_user': dm.user.username if dm.user else None,
                        'is_active': dm.is_active,
                        'created_at': dm.created_at.isoformat(),
                    }
                    record['discord'] = discord_data
                
                if user_data['ranking']:
                    record['ranking'] = user_data['ranking']
                
                if user_data.get('discord_role_status'):
                    record['has_top_10_role'] = user_data['discord_role_status']['has_top_10_role']
                
                output_data.append(record)
            
            self.stdout.write(json.dumps(output_data, indent=2))
        
        elif output_format == 'csv':
            import csv
            import sys
            
            writer = csv.writer(sys.stdout)
            writer.writerow([
                'Discord User ID', 'Discord Username', 'Discord Display Name',
                'Linked Name', 'Linked Twitter', 'Latest Rank', 'Week Start',
                'Has Top 10 Role', 'Django User'
            ])
            
            for key, user_data in sorted(combined_users.items()):
                # Get current Discord username/display from API if available
                if user_data['discord']:
                    if user_data.get('discord_role_status'):
                        discord_username = user_data['discord_role_status']['discord_username']
                        discord_display = user_data['discord_role_status']['discord_display_name']
                    else:
                        discord_username = user_data['discord'].discord_username
                        discord_display = user_data['discord'].discord_display_name
                else:
                    discord_username = ''
                    discord_display = ''
                
                row = [
                    user_data['discord'].discord_user_id if user_data['discord'] else '',
                    discord_username,
                    discord_display,
                    user_data['discord'].linked_name if user_data['discord'] else '',
                    user_data['discord'].linked_twitter_handle if user_data['discord'] else '',
                    user_data['ranking']['rank'] if user_data['ranking'] else '',
                    user_data['ranking']['week_start_date'] if user_data['ranking'] else '',
                    'YES' if user_data.get('discord_role_status') and user_data['discord_role_status'].get('has_top_10_role', False) else 'NO',
                    user_data['discord'].user.username if user_data['discord'] and user_data['discord'].user else '',
                ]
                writer.writerow(row)
